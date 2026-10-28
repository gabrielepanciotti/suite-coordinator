# Coordinator

Coordinator is a self-hosted Telegram bridge for running project-scoped AI sessions, routing work between local agents, and delivering scheduled notifications from one forum-style group.

## Features

- One Telegram topic per project and agent session
- Project and user-profile access controls
- Scheduled notifications and health checks
- Optional Network/Life registry and agent-bus integration
- React notification dashboard and PostgreSQL-backed optional persistence

## Requirements

- Python 3.13
- Node.js 20 (only for the frontend)
- Docker Compose for the containerized deployment
- A Telegram bot token and a Claude or compatible CLI login for interactive sessions

## Quickstart

```bash
git clone <your-fork-url> coordinator
cd coordinator
cp .env.example .env
# Edit .env and set TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, and CLAUDE_HOST_DIR.
docker compose -f deploy/docker-compose.example.yml up --build
```

For local backend development, create a virtual environment and install `requirements-dev.txt`, then run `pytest -q`. For the dashboard, run `npm ci && npm run dev` in `frontend/`. The full private deployment compose files are deliberately not part of the public release; the example compose file is the supported starting point.

## Tests

```bash
python -m pytest -q
cd frontend && npm ci && npm test -- --pool=forks --poolOptions.forks.maxForks=2 --poolOptions.forks.minForks=1
```

The optional Life framework is checked out alongside this repository in CI. Tests that require its sibling packages install `Life/packages/shared` and `Life/packages/backend-core` first.

## Configuration

| Area | Environment variables |
| --- | --- |
| Telegram | `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`, `PROFILE_OWNER_IDS`, `PROFILE_MEMBER_IDS` |
| AI runtime | `CLAUDE_BIN`, `CLAUDE_HOST_DIR`, `CLAUDE_MODEL`, `AI_BACKEND`, `OPENCODE_BIN`, `OPENCODE_MODEL` |
| Notifications | `HA_BASE_URL`, `HA_TOKEN`, `NOTIF_PATH`, `SCHEDULER_ENABLED` |
| Network | `HUB_URL`, `HUB_API_KEY`, `HUB_TENANTS`, `HUB_TELEGRAM_AGENT` |
| Life registry | `LIFE_BACKEND_URL`, `LIFE_DEVICE`, `LIFE_ADMIN_TOKEN` |
| Database and API | `DATABASE_URL`, `COORDINATOR_API_TOKEN`, `OWNERSHIP_TOKEN` |

See [`.env.example`](.env.example) for the complete documented configuration.

## License

MIT — see [LICENSE](LICENSE).
