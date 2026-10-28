# --- Printing Press CLI: tailscale-pp-cli (Life#95) --------------------------
# The Life admin agent (share a node, audit who has access, revoke, verify)
# runs inside this container and drives `tailscale-pp-cli`. The catalog entry
# is a Go program, so it is built in a throwaway toolchain stage and only the
# static binary lands in the runtime image (Python stays the only runtime
# toolchain). The ref is the catalog's released `source_commit` — pinned on
# purpose: `@latest` would let a later rebuild silently ship a different
# binary (catalog: printing-press `tailscale`, release 2026.10.1).
FROM golang:1.26.8-bookworm AS pp-cli-builder
ARG TAILSCALE_PP_REF=4a2759019541195b426f7435f46dabe57c94f2f6
ENV CGO_ENABLED=0 GOBIN=/out
RUN go install github.com/mvanhorn/printing-press-library/library/cloud/tailscale/cmd/tailscale-pp-cli@${TAILSCALE_PP_REF}

FROM python:3.13.6-slim

# Node 22 (required by @anthropic-ai/claude-code >= 2.1.198) + curl/ca-certificates.
# On Node 20 npm does not fail: it silently installs the newest release whose
# `engines` still match, so the image stayed frozen on 2.1.197 through every
# rebuild while the hosts moved on.
# NOTE: git is NOT installed by default. On WINDOWS hosts the workspaces are bind
# mounts over Docker Desktop's FS bridge, where metadata is ~1 file/s: `git status`
# on a 562-file repo measured 530s. With git on PATH, Claude Code's automatic
# startup `git status` would blow past the worker idle timeout and kill every turn.
# Without git, that probe is skipped (fast turns); commit/branch info is read from
# .git files directly. Code updates + commits happen host-side (native git, fast).
# On LINUX hosts the mounts are native and a dev worker NEEDS git (commit/push):
# opt in per-device with INSTALL_GIT=1 in .env (wired via compose build args).
# openssh-client ships with git, not separately: a remote reached over SSH (the
# deployer on a Windows box pulls from an SSH mirror) needs it, and git's error for a missing
# ssh binary is the same "Could not read from remote repository" you get for a
# wrong key — easy to chase in the wrong direction.
# The flag also brings in the `gh` CLI: several nodes keep their backlog in
# GitHub Issues, and an agent asked "what is open?" from Telegram cannot answer
# with git alone -- issues live behind the REST API, so gh + GH_TOKEN in .env.
# SSH keys authenticate git, NOT the API: without a token a private repo answers
# 404, which reads like "repo missing" instead of "not authenticated".
ARG INSTALL_GIT=0
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates gnupg ffmpeg tesseract-ocr tesseract-ocr-ita \
 && if [ "$INSTALL_GIT" = "1" ]; then \
      apt-get install -y --no-install-recommends git openssh-client \
   && install -m 0755 -d /etc/apt/keyrings \
   && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli.gpg \
   && chmod a+r /etc/apt/keyrings/githubcli.gpg \
   && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
   && apt-get update \
   && apt-get install -y --no-install-recommends gh; \
    fi \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y nodejs \
 && rm -rf /var/lib/apt/lists/*

# Docker CLI + compose plugin: the worker can deploy (docker compose) against the
# host engine via the mounted socket (see docker-compose.prod.yml).
RUN install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
  && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin postgresql-client \
  && rm -rf /var/lib/apt/lists/*

# tailscale-pp-cli from the throwaway Go stage above: on app's PATH
# (/usr/local/bin) so the woken Life sessions can run it. Credentials arrive
# from env_file (.env: TAILSCALE_OAUTH_CLIENT_ID/SECRET); its config/cache live
# under the writable app home.
COPY --from=pp-cli-builder /out/tailscale-pp-cli /usr/local/bin/tailscale-pp-cli

# Claude Code CLI + OpenCode CLI (AI_BACKEND=opencode, see claude_runner.py)
# The ADD fetches the registry's "latest" manifest on every build: its content
# changes when a new CLI is published, which invalidates the cache of the npm
# layer below. Without it the layer stays cached and every rebuild ships the CLI
# of the day it was first built. Runtime freshness: scripts/update-claude-cli.sh.
ADD https://registry.npmjs.org/@anthropic-ai/claude-code/latest /tmp/claude-code-latest.json
RUN npm install -g @anthropic-ai/claude-code@latest opencode-ai@latest \
 && rm -f /tmp/claude-code-latest.json

# Non-root user: --permission-mode bypassPermissions refuses to run as root
RUN useradd -m app

# Writable CLI state under app's home: tailscale-pp-cli keeps its config.toml
# and sqlite cache in the XDG dirs. `useradd -m` already owns /home/app; make
# them explicit so the CLI is never read-only (Life#95).
RUN install -d -o app -g app /home/app/.config /home/app/.local/share /home/app/.cache

WORKDIR /app

# Python deps
# I pacchetti in vendor/ (submodule dell'installazione, es. la libreria di auth
# che GRAPH_APP_AUTH nomina) sono opzionali: si installa solo quello che c'e'.
# Un clone senza le credenziali di quei repo ha le cartelle vuote e la build
# prosegue: graph_mailer spegne il solo percorso app-only.
COPY requirements.txt .
COPY vendor/ ./vendor/
RUN pip install --no-cache-dir -r requirements.txt \
 && for d in vendor/*/; do \
      if [ -f "${d}pyproject.toml" ] || [ -f "${d}setup.py" ]; then \
        pip install --no-cache-dir -e "./${d}"; \
      fi; \
    done

# Ingest tooling deps (tools/fetch_url.py: trafilatura, yt-dlp, ...). The tool
# itself is bind-mounted at runtime (./tools:/app/tools:ro in compose).
COPY tools/requirements-ingest.txt ./tools/requirements-ingest.txt
RUN pip install --no-cache-dir -r tools/requirements-ingest.txt

# Pre-bake the faster-whisper 'small' model into the image so the gonzalo session
# never downloads it at runtime (offline-safe, no first-reel latency). HF_HOME is a
# shared cache dir, chowned to the app user that runs the transcription.
ENV HF_HOME=/opt/models
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')" \
 && chown -R app /opt/models

# App code. All root modules are baked so the image is self-contained for hosts
# that do NOT bind-mount the bridge sources (e.g. the worker via docker-compose.prod.yml,
# which otherwise crashes at `import a2a_client`). The bot still bind-mounts these RO
# in docker-compose.yml (live-reload without rebuild); the mount shadows the baked copy.
COPY *.py ./
# Scheda-nodo static manifest (served by GET /api/overview; regenerated by the
# scheda-nodo skill / suite_core.nodes node-overview).
COPY overview.manifest.json ./
# Installation defaults read by local_defaults.py (see locale/README.md).
COPY locale ./locale
# Optional per-node DB layer (db.session used by the /api/overview live section).
COPY db ./db

# Writable state dir (topic bindings + sessions persisted here, see STATE_PATH)
RUN mkdir -p /app/data && chown -R app /app/data

# The XDG `install -d` right after `useradd` left /home/app/.local owned by
# root: `install -d -o app` owns only the LAST path component. opencode then
# died on `mkdir ~/.local/state` (EACCES) on every turn. Fixed here, after
# `COPY *.py`, on purpose: that layer changes on every code edit, so this costs
# nothing, while touching the earlier line would invalidate the pip and whisper
# layers and re-download ~1 GB — enough to hang the minipc's USB Wi-Fi.
# Guarded by tests/test_dockerfile_home.py.
# opencode's session store lives in ~/.local/share/opencode and is a named
# volume in compose (opencode_data): pre-create it app-owned so Docker seeds
# the empty volume with app ownership instead of a root-owned mount point.
RUN install -d -o app -g app /home/app/.local /home/app/.local/state \
    /home/app/.local/share/opencode

# User-owned npm prefix, first on PATH: scripts/update-claude-cli.sh installs a
# newer CLI here at container start (the baked one in /usr is root-owned).
ENV PATH=/home/app/.npm-cli/bin:$PATH

USER app
CMD ["python", "bot.py"]
