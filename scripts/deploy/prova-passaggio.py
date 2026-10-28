#!/usr/bin/env python3
"""Live handover test of the one bot's poller ladder (#83 F5, conv 720).

Runs ON THE HOST of dev-linux, detached (``setsid nohup``): it recreates
coordinator-bot, which kills any session the bot woke, so it first waits for
``running_wakes`` to drain. Then:

1. recreate the bot (new .env / ladder) and wait until it polls;
2. arm the peer (``wants_ownership`` true, it stays in standby);
3. handoff: dev-linux -> peer, then back (peer releases first: no overlap);
4. stop/start: ``docker stop`` dev-linux, the peer takes over after the
   failed probes, ``docker start`` and the peer yields at its next probe;
5. count the 409 Conflict lines on both sides and, with ``--conv``, post the
   report on the bus as coordinator-dev-linux.

Usage: scripts/deploy/prova-passaggio.py --peer minipc \
           --peer-url http://100.115.161.123:8113 --peer-ssh minipc \
           --peer-container coordinator-worker-poller [--conv 720]
Log: data/prova-passaggio.log
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOCAL = "http://127.0.0.1:8013"
ME = "pg1-vus-rmdevsrv"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def status(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url + "/ownership", timeout=5) as r:
            return json.load(r)
    except Exception:
        return None


def handoff(url: str, target: str) -> dict | None:
    req = urllib.request.Request(
        url + "/ownership/handoff", data=json.dumps({"target_device": target}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def wait_for(pred, timeout: float, step: float = 2.0) -> float | None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return round(time.time() - t0, 1)
        time.sleep(step)
    return None


def polling(url: str) -> bool:
    return bool((status(url) or {}).get("is_polling"))


def running_wakes() -> int:
    try:
        data = json.loads((REPO / "data/hub_topics.json").read_text(encoding="utf-8"))
        return len(data.get("running_wakes") or {})
    except Exception:
        return -1  # unreadable never counts as zero


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, **kw)


def conflicts_local(since: str) -> int:
    out = sh(["docker", "logs", "--since", since, "coordinator-bot"])
    return sum("Conflict" in line for line in (out.stdout + out.stderr).splitlines())


def conflicts_peer(ssh: str, container: str, since: str) -> int:
    out = sh(["ssh", ssh, f"docker logs --since {since} {container} 2>&1 | grep -c Conflict"])
    try:
        return int(out.stdout.strip() or 0)
    except ValueError:
        return -1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer", required=True)
    ap.add_argument("--peer-url", required=True)
    ap.add_argument("--peer-ssh", required=True)
    ap.add_argument("--peer-container", default="coordinator-worker-poller")
    ap.add_argument("--conv", type=int)
    ap.add_argument("--max-wait", type=int, default=3600)
    a = ap.parse_args()
    peer, purl = a.peer, a.peer_url.rstrip("/")
    report: list[str] = []
    ok = True

    waited = 0
    while running_wakes() != 0 and waited < a.max_wait:
        time.sleep(10)
        waited += 10
    log(f"turni in corso: {running_wakes()} dopo {waited}s")

    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    up = sh(["docker", "compose", "up", "-d", "--force-recreate", "coordinator-bot"])
    log(f"ricreo bot exit {up.returncode} {up.stderr.strip()[-200:]}")
    t = wait_for(lambda: polling(LOCAL), 240)
    st = status(LOCAL) or {}
    report.append(f"Ricreo dev-linux: polla dopo {t}s, priorità {st.get('priority')}, "
                  f"scala {' > '.join(st.get('ladder') or [])}")
    ok &= t is not None and st.get("priority") == 50

    handoff(purl, peer)  # arm: wants true, dev-linux is stronger -> standby
    time.sleep(20)
    p = status(purl) or {}
    report.append(f"Peer armato: wants {p.get('wants_ownership')}, polla {p.get('is_polling')}")
    ok &= p.get("wants_ownership") is True and p.get("is_polling") is False

    # --- handoff there and back ------------------------------------------------
    handoff(LOCAL, peer)
    t = wait_for(lambda: polling(purl), 90)
    report.append(f"Handoff dev-linux -> {peer}: {peer} polla dopo {t}s "
                  f"(epoch {(status(purl) or {}).get('lease', {}).get('epoch')})")
    ok &= t is not None
    time.sleep(60)
    handoff(purl, ME)       # the peer releases first...
    handoff(LOCAL, ME)      # ...then dev-linux wants it back
    t = wait_for(lambda: polling(LOCAL) and not polling(purl), 90)
    handoff(purl, peer)     # re-arm the peer as standby
    report.append(f"Ritorno a dev-linux: riprende dopo {t}s "
                  f"(epoch {(status(LOCAL) or {}).get('lease', {}).get('epoch')})")
    ok &= t is not None

    # --- stop / start ----------------------------------------------------------
    if wait_for(lambda: running_wakes() == 0, 900, 10) is None:
        report.append("Stop/start saltato: turni del bus in corso da 15 minuti")
        ok = False
    else:
        sh(["docker", "stop", "coordinator-bot"])
        t = wait_for(lambda: polling(purl), 120)
        report.append(f"docker stop dev-linux: {peer} prende dopo {t}s")
        ok &= t is not None
        time.sleep(30)
        sh(["docker", "start", "coordinator-bot"])
        t_yield = wait_for(lambda: not polling(purl), 120)
        t_back = wait_for(lambda: polling(LOCAL), 180)
        report.append(f"docker start dev-linux: {peer} cede dopo {t_yield}s, "
                      f"dev-linux riprende dopo altri {t_back}s")
        ok &= t_yield is not None and t_back is not None

    time.sleep(20)
    c_local = conflicts_local(since)
    c_peer = conflicts_peer(a.peer_ssh, a.peer_container, since)
    report.append(f"Righe 409 Conflict dal ricreo: dev-linux {c_local}, {peer} {c_peer}")
    # One overlap of at most a probe interval is allowed: the holder yields at its
    # first 409 (D3). "Repeated" is what the test rules out.
    ok &= 0 <= c_local <= 3 and 0 <= c_peer <= 3
    d, p = status(LOCAL), status(purl)
    report.append(f"/ownership dev-linux: {json.dumps(d)}")
    report.append(f"/ownership {peer}: {json.dumps(p)}")
    ok &= bool(d and d.get("is_polling")) and bool(p and not p.get("is_polling"))

    body = ("Prova di passaggio dal vivo " + ("✅ superata" if ok else "❌ NON superata")
            + " (script scripts/deploy/prova-passaggio.py, eseguito dopo la fine del mio turno"
            " perché ricreare il bot avrebbe ucciso la sessione)\n\n- " + "\n- ".join(report))
    log(body)
    if a.conv:
        out = sh(["python3", str(Path.home() / ".claude/scripts/hub_send.py"), "reply",
                  str(a.conv), "--body", body])
        log(f"hub_send exit {out.returncode} {out.stdout.strip()[-200:]} {out.stderr.strip()[-200:]}")


if __name__ == "__main__":
    main()
