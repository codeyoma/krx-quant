#!/usr/bin/env bash
# Let the server reboot itself when its kernel stops, and keep the journal on disk (#385).
#
# On 2026-10-28 the OS hung from about 00:30 to 12:13 KST: Lightsail's instance status check
# failed every minute while the system check passed, the picture of a kernel that panicked and
# waits forever (Ubuntu's kernel.panic is 0). These settings reboot it 10 seconds after a panic,
# and make an oops or a soft lockup a panic. The journal of the boot before a hang stays on disk
# (journalctl -b -1). A hang these do not catch is the external watchdog's
# (.github/workflows/server-watchdog.yml, docs/operations.md "서버가 멈추면 (#385)").
#
# setup-server.sh runs it. It can run alone too, from the repository checkout:
#
#   sudo deploy/kernel-recovery.sh
#
# Safe to run again: it never restarts Docker or the engine, and restarts systemd-journald only
# when its settings change. KRX_SYSROOT writes the files under another root (the tests).
set -euo pipefail

root=${KRX_SYSROOT:-}
if [[ -z "$root" && $EUID -ne 0 ]]; then
  echo "kernel-recovery: run with sudo" >&2
  exit 1
fi

# Not kernel.hung_task_panic: in the nightly training a task can wait in D state for longer than
# the 120 s hung-task timeout while it swaps or the disk is slow, and that would reboot a working
# server.
sysctl_file=$root/etc/sysctl.d/91-krx-quant-recovery.conf
mkdir -p "$(dirname "$sysctl_file")"
cat >"$sysctl_file" <<'CONF'
# krx-quant (#385, deploy/kernel-recovery.sh): reboot 10 seconds after a kernel panic, and make
# an oops or a soft lockup a panic. Not kernel.hung_task_panic: a task blocked on swap or a slow
# disk during the nightly training would reboot a working server.
kernel.panic = 10
kernel.panic_on_oops = 1
kernel.softlockup_panic = 1
CONF
sysctl -q -p "$sysctl_file"
echo "kernel: panic 10, panic_on_oops 1, softlockup_panic 1 ($sysctl_file)"

journald_file=$root/etc/systemd/journald.conf.d/90-krx-quant.conf
journald_conf='# krx-quant (#385, deploy/kernel-recovery.sh): keep the journal on disk, so the boot
# before a hang can be read (journalctl -b -1), in at most 200 MB.
[Journal]
Storage=persistent
SystemMaxUse=200M'
mkdir -p "$root/var/log/journal" "$(dirname "$journald_file")"
if [[ "$(cat "$journald_file" 2>/dev/null || true)" != "$journald_conf" ]]; then
  printf '%s\n' "$journald_conf" >"$journald_file"
  # The directory's group and ACLs as systemd ships them, then journald reads the new settings
  # and moves this boot's journal from /run to the disk. Only journald restarts.
  systemd-tmpfiles --create --prefix /var/log/journal
  systemctl restart systemd-journald
  journalctl --flush
  echo "journal: kept on disk, at most 200 MB ($journald_file)"
else
  echo "journal: kept on disk already ($journald_file)"
fi
