#!/usr/bin/env bash
# Update the engine to the latest main outside trading hours; go back if it does not start.
#
# The krx-quant-update timer runs it every day at 06:00 KST as the checkout's owner
# (deploy/install-auto-update.sh). By hand, from anywhere: deploy/auto-update.sh
#
# 1. Weekdays 07:50-20:10 KST it refuses: KRX and NXT trade, and a restart leaves a gap
#    in the recording.
# 2. It fetches origin/main. Nothing to do if the checkout has it, or if that commit
#    failed to start before (.auto-update-failed) and main has not moved since.
# 3. It fast-forwards, rebuilds and restarts the engine.
# 4. It waits for the Engine API to answer (3 minutes). If it does not, it goes back to
#    the commit before, rebuilds, and remembers the commit that failed.
# 5. It reports to Telegram (krx-quant notify).
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"
tries=${KRX_UPDATE_TRIES:-36}
pause=${KRX_UPDATE_PAUSE:-5}
failed_file=.auto-update-failed

read -r weekday hhmm <<<"${KRX_UPDATE_NOW:-$(TZ=Asia/Seoul date '+%u %H%M')}"
if ((weekday <= 5 && 10#$hhmm >= 750 && 10#$hhmm < 2010)); then
  echo "auto-update: market hours (weekdays 07:50-20:10 KST); not updating" >&2
  exit 1
fi

notify() {
  docker compose run --rm --no-deps engine notify "$1" >/dev/null 2>&1 ||
    echo "auto-update: could not send to Telegram: $1" >&2
}

healthy() {
  local _
  for _ in $(seq "$tries"); do
    sleep "$pause"
    if docker compose exec -T engine python -c \
      'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/v1/status", timeout=5)' \
      >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

restart() {
  docker compose up -d --build && healthy
}

# Anything else that fails (GitHub unreachable, a checkout that cannot fast-forward) is
# reported too; the engine keeps running what it ran.
trap 'notify "자동 업데이트 오류: ${BASH_COMMAND} (줄 ${LINENO}). 엔진은 그대로입니다."' ERR

git fetch -q origin main
current=$(git rev-parse HEAD)
target=$(git rev-parse origin/main)
if [[ "$current" == "$target" ]]; then
  echo "auto-update: up to date (${current:0:7})"
  exit 0
fi
if [[ -f "$failed_file" && "$(cat "$failed_file")" == "$target" ]]; then
  echo "auto-update: ${target:0:7} did not start before; waiting for a newer main"
  exit 0
fi
changes=$(git log --format='- %s' "$current..$target" | head -n 10)
git merge -q --ff-only "$target"

if restart; then
  rm -f "$failed_file"
  docker image prune -f >/dev/null || true
  echo "auto-update: ${current:0:7} -> ${target:0:7}"
  notify "자동 업데이트: ${current:0:7} → ${target:0:7}"$'\n'"$changes"
  exit 0
fi

echo "auto-update: ${target:0:7} did not start; going back to ${current:0:7}" >&2
echo "$target" >"$failed_file"
git reset -q --hard "$current"
if restart; then
  state="${current:0:7}로 되돌렸습니다"
else
  state="${current:0:7}로 되돌렸지만 엔진이 응답하지 않습니다. 확인이 필요합니다"
fi
notify "자동 업데이트 실패: ${target:0:7}이 시작되지 않아 $state."
exit 1
