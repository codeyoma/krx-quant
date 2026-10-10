#!/usr/bin/env bash
# Install the daily update timer: deploy/auto-update.sh at 06:00 KST, as the checkout's
# owner. Run once on the server, from the repository checkout (setup-server.sh runs it):
#
#   sudo deploy/install-auto-update.sh
#
# Safe to run again. To stop updating: sudo systemctl disable --now krx-quant-update.timer
# Its runs: journalctl -u krx-quant-update
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "install-auto-update: run with sudo" >&2
  exit 1
fi
repo=$(cd "$(dirname "$0")/.." && pwd)
owner=${SUDO_USER:-ubuntu}

cat >/etc/systemd/system/krx-quant-update.service <<UNIT
[Unit]
Description=Update the krx-quant engine to the latest main
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$owner
WorkingDirectory=$repo
ExecStart=$repo/deploy/auto-update.sh
UNIT

# Not Persistent: a run missed while the server was off must not start at boot, which
# may be in trading hours. 06:00 is after the night's training (21:30) and before NXT
# and the morning model swap (08:00).
cat >/etc/systemd/system/krx-quant-update.timer <<UNIT
[Unit]
Description=Update the krx-quant engine every day at 06:00 KST

[Timer]
OnCalendar=*-*-* 06:00:00 Asia/Seoul
Persistent=false

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now krx-quant-update.timer
systemctl list-timers krx-quant-update.timer --no-pager
