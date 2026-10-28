#!/usr/bin/env bash
# Installs the Coordinator watchdog as a systemd USER timer (every minute, Linger=yes
# keeps it alive without a login). Idempotent.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/coordinator-watchdog.service" <<EOF
[Unit]
Description=Coordinator bot external watchdog (Coordinator#76)

[Service]
Type=oneshot
ExecStart=$REPO/scripts/watchdog/coordinator-watchdog.sh
EOF
cat > "$UNIT_DIR/coordinator-watchdog.timer" <<EOF
[Unit]
Description=Coordinator bot watchdog, every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now coordinator-watchdog.timer
systemctl --user list-timers coordinator-watchdog.timer --no-pager
