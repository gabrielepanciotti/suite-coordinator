#!/usr/bin/env bash
# Recreate coordinator-bot without killing the sessions it woke (#73).
#
# A session woken by the bot runs INSIDE the bot container as a child of it.
# `docker compose up -d bot` from that session kills the session itself (exit
# 137, new container left in Created) — and with the old script a plain
# setsid+nohup did NOT help either: the detached child lived in the same
# container PID namespace and died with it. So the recreate is delegated to a
# THROWAWAY HELPER CONTAINER that mounts the Docker socket and this repo, and
# therefore survives the bot it recreates. This script:
#   1. if it is itself running inside a container, spawns that helper (detached)
#      and returns;  on the host it detaches with setsid+nohup;
#   2. the detached copy waits until no wake is marked running in
#      data/hub_topics.json (max RICREA_MAX_WAIT seconds, default 1800);
#   3. runs `docker compose up -d <services>` (default: coordinator-bot);
#   4. VERIFIES the service(s) actually reached `running`: a failed bind mount
#      leaves the container in `Created` and, without this check, the failure is
#      mute (the 2026-10-14 incident: `error mounting /repo/profiles.py`).
# Whatever is still running at the deadline is not lost: the bot requeues it at
# boot (hub_store.requeue_interrupted_wakes).
#
# HOST PATHS (the other half of #73): inside a container, relative bind sources
# in the compose file (`./profiles.py`) resolve against the CONTAINER path, and
# the daemon then looks for them ON THE HOST — `/repo/profiles.py ... not a
# directory`. The helper mounts the repo at its real host path and runs compose
# from there, so host and container paths coincide and relative sources are
# correct. `host_repo_path` resolves that host path from this container's own
# bind mounts.
#
# Usage: scripts/deploy/ricrea-bot.sh [--build|--force-recreate|--...] [service...]
#   Options starting with -- go to `docker compose up -d` as they are. With only
#   bind-mounted .py changed the config is identical and a plain `up -d` is a
#   no-op: use --force-recreate.
# Log:   data/ricrea-bot.log
set -u
cd "$(dirname "$0")/../.."
REPO="$PWD"
LOG="$REPO/data/ricrea-bot.log"

# The host path of $REPO. On the host it is already $REPO; inside a container,
# find the bind mount whose Destination is an ancestor of $REPO and append the
# remaining suffix. Prints nothing when it cannot be resolved.
host_repo_path() {
    [ -f /.dockerenv ] || { printf '%s' "$REPO"; return; }
    MOUNTS_JSON="$(docker inspect "$(cat /etc/hostname 2>/dev/null || hostname)" \
        --format '{{json .Mounts}}' 2>/dev/null)" REPO_ARG="$REPO" python3 - <<'PY'
import json, os
repo = os.path.normpath(os.environ["REPO_ARG"])
try:
    mounts = json.loads(os.environ.get("MOUNTS_JSON") or "[]")
except Exception:
    mounts = []
best = None
for m in mounts or []:
    if m.get("Type") != "bind":
        continue
    dst = os.path.normpath(m.get("Destination") or "")
    if dst and dst != "/" and (repo == dst or repo.startswith(dst + "/")):
        if best is None or len(dst) > len(best[0]):
            best = (dst, m.get("Source") or "")
if best:
    suffix = repo[len(best[0]):].strip("/")
    print(os.path.join(best[1], suffix) if suffix else best[1])
PY
}

# Distinguish the initial invocation from the detached copy: the detached copy
# (host child or helper container) sets RICREA_DETACHED=1.
if [ -z "${RICREA_DETACHED:-}" ]; then
    if [ -f /.dockerenv ] && [ ! -S /var/run/docker.sock ]; then
        # coordinator-bot has no docker socket any more (#89): a recreate is
        # the host's job, from a terminal or a bus wake (those run on the host).
        echo "❌ niente docker in questo container (#89): lancia ricrea-bot.sh dall'host"
        exit 2
    fi
    # A module the code imports but the compose does not mount crashes the
    # new container at import (#103): refuse before touching the running one.
    if ! python3 "$REPO/scripts/deploy/check_mounts.py"; then
        echo "❌ ricreo rifiutato: moduli non montati (sopra). Aggiungili al compose e rilancia"
        exit 3
    fi
    if [ -f /.dockerenv ]; then
        # Inside a container: only a SEPARATE container survives the recreate,
        # and it is also what makes the compose bind sources host-correct.
        host_repo="$(host_repo_path)"
        [ -n "$host_repo" ] || { host_repo="$REPO"; echo "warn: host path non risolto, uso $REPO"; }
        image="$(docker inspect "$(cat /etc/hostname 2>/dev/null || hostname)" \
            --format '{{.Config.Image}}' 2>/dev/null)"
        [ -n "$image" ] || image="coordinator-bot"
        # Forward the tunables if the caller set them (so the helper's wait and
        # verify timeouts can be controlled from here).
        fwd=()
        for v in RICREA_MAX_WAIT RICREA_VERIFY_TIMEOUT; do
            [ -n "${!v:-}" ] && fwd+=(-e "$v=${!v}")
        done
        # The helper runs as the image's `app` user, which is NOT in the host
        # docker group: without this the socket mount is there but every
        # `docker` call gets "permission denied while trying to connect to the
        # docker API" (masked in a dry run by a pre-daemon validation error —
        # it only showed up on the first real recreate, 2026-10-14 20:44).
        dgid="$(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo 0)"
        docker rm -f coord-recreate >/dev/null 2>&1 || true
        docker run -d --rm \
            --name coord-recreate \
            --entrypoint bash \
            --group-add "$dgid" \
            -e RICREA_DETACHED=1 \
            "${fwd[@]}" \
            -v /var/run/docker.sock:/var/run/docker.sock \
            -v "$host_repo:$host_repo" \
            -w "$host_repo" \
            "$image" \
            "$host_repo/scripts/deploy/ricrea-bot.sh" "$@" >/dev/null
        echo "ricreo delegato al container helper coord-recreate (host path $host_repo)."
        echo "log: $LOG / docker logs coord-recreate"
        exit 0
    fi
    # On the host there is no container to die with, so setsid+nohup suffices.
    RICREA_DETACHED=1 setsid nohup "$0" "$@" >>"$LOG" 2>&1 </dev/null &
    echo "ricreo del bot staccato (pid $!): parte quando i turni svegliati finiscono. Log: $LOG"
    exit 0
fi

# Detached copy: everything below is logged, wherever it runs.
exec >>"$LOG" 2>&1

build=()
services=()
for arg in "$@"; do
    case "$arg" in
        --*) build+=("$arg") ;;
        *) services+=("$arg") ;;
    esac
done
[ ${#services[@]} -eq 0 ] && services=(coordinator-bot)

max_wait="${RICREA_MAX_WAIT:-1800}"
verify_timeout="${RICREA_VERIFY_TIMEOUT:-60}"
state="$REPO/data/hub_topics.json"
echo "$(date -Is) richiesto ricreo di ${services[*]} ${build[*]}"

# Prints the number of wakes in flight, "?" if the state cannot be read: an
# unreadable count must never pass for zero.
running() {
    STATE="$state" python3 - <<'PY' 2>/dev/null || echo "?"
import json, os, sys
with open(os.environ["STATE"], encoding="utf-8") as fh:
    r = json.load(fh).get("running_wakes") or {}
print(len(r))
for k, v in r.items():
    print("  %s #%s" % (k, v.get("conv_id")), file=sys.stderr)
PY
}

waited=0
while :; do
    n="$(running)"
    [ "$n" = "0" ] && break
    if [ "$waited" -ge "$max_wait" ]; then
        echo "$(date -Is) dopo ${waited}s restano $n turni: ricreo comunque, ripartono al boot"
        running >/dev/null
        break
    fi
    [ $((waited % 60)) -eq 0 ] && echo "$(date -Is) attendo $n turni in corso"
    sleep 10
    waited=$((waited + 10))
done

docker compose up -d "${build[@]}" "${services[@]}"
rc=$?
echo "$(date -Is) compose up -d exit $rc"
if [ "$rc" -ne 0 ]; then
    echo "$(date -Is) ❌ ricreo fallito: docker compose up -d exit $rc"
    exit 1
fi

# Post-recreate verification: the service must be RUNNING, not merely created.
# `docker compose ps -a` so a container stuck in `Created`/`Exited` is visible.
verify_one() {
    PS_JSON="$(docker compose ps -a --format json "$1" 2>/dev/null)" SVC="$1" python3 - <<'PY'
import json, os, sys
svc = os.environ["SVC"]
rows = []
for line in (os.environ.get("PS_JSON") or "").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        parsed = json.loads(line)
    except ValueError:
        continue
    rows.extend(parsed if isinstance(parsed, list) else [parsed])
if not rows:
    print("BAD %s: nessun container" % svc)
    sys.exit(1)
for r in rows:
    state = (r.get("State") or r.get("state") or "").lower()
    health = (r.get("Health") or r.get("health") or "").lower()
    name = r.get("Name") or r.get("name") or svc
    if state == "running" and health != "unhealthy":
        print("OK %s (%s%s)" % (name, state, ("/" + health) if health else ""))
        continue
    print("BAD %s=%s%s" % (name, state, ("/" + health) if health else ""))
    sys.exit(1)
sys.exit(0)
PY
}

for svc in "${services[@]}"; do
    vwaited=0
    while :; do
        if out="$(verify_one "$svc")"; then
            echo "$(date -Is) verify $svc: $out"
            break
        fi
        if [ "$vwaited" -ge "$verify_timeout" ]; then
            echo "$(date -Is) ❌ verifica $svc fallita (${vwaited}s): container non running"
            echo "   stato: $(docker compose ps -a --format json "$svc" 2>/dev/null | tr '\n' ' ')"
            echo "   ultime righe di log:"
            docker compose logs --tail=30 "$svc" 2>&1 | sed 's/^/     /'
            exit 1
        fi
        sleep 3
        vwaited=$((vwaited + 3))
    done
done

echo "$(date -Is) ✅ ricreo finito: ${services[*]} running"
