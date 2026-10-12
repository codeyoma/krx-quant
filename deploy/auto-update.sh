#!/usr/bin/env bash
# Update the engine to the latest main outside trading hours; go back if it does not start.
#
# The krx-quant-update timer runs it every day at 06:00 KST as the checkout's owner
# (deploy/install-auto-update.sh). By hand, from anywhere: deploy/auto-update.sh
#
# 1. Weekdays 07:50-20:10 KST it refuses: KRX and NXT trade, and a restart leaves a gap
#    in the recording.
# 2. It fetches origin/main. Nothing to do if the engine was last checked to start from it
#    (.auto-update-deployed), or if that commit failed to start before
#    (.auto-update-failed) and main has not moved since.
# 3. It fast-forwards, rebuilds and restarts the engine.
# 4. It waits for the Engine API to answer (3 minutes). If it does not, it goes back to
#    the commit before, rebuilds, and remembers the commit that failed.
# 5. It pushes the new image to ECR for the nightly training on EC2 Spot, if that is set
#    up (deploy/push-image.sh, #23). A push that failed is tried again on the next run,
#    even with nothing new on main (.auto-update-push-failed).
# 6. It reports to Telegram (krx-quant notify).
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"
tries=${KRX_UPDATE_TRIES:-36}
pause=${KRX_UPDATE_PAUSE:-5}
failed_file=.auto-update-failed
deployed_file=.auto-update-deployed
push_failed_file=.auto-update-push-failed

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
    # The port the engine listens on inside its container (compose sets KRX_API_PORT).
    if docker compose exec -T engine python -c \
      'import os, urllib.request
port = os.environ.get("KRX_API_PORT", "8080")
urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/status", timeout=5)' >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

# The image is told the commit it is built from (KRX_COMMIT): the engine shows and records it.
restart() {
  KRX_COMMIT=$(git rev-parse HEAD) docker compose up -d --build && healthy
}

# The running image to ECR for the Spot training; remembered when it fails, for the next run.
push_image() {
  if deploy/push-image.sh; then
    rm -f "$push_failed_file"
  else
    touch "$push_failed_file"
    return 1
  fi
}

# Anything else that fails (GitHub unreachable, a checkout that cannot fast-forward) is
# reported too; the engine keeps running what it ran.
trap 'notify "자동 업데이트 오류: ${BASH_COMMAND} (줄 ${LINENO}). 엔진은 그대로입니다."' ERR

git fetch -q origin main
target=$(git rev-parse origin/main)
# The commit the running engine was last checked to start from. The checkout may be ahead
# of it if a run was cut short (a reboot) after moving it; then the run is done again.
# Before the first run, what is checked out is taken as running.
deployed=$(cat "$deployed_file" 2>/dev/null || git rev-parse HEAD)
if [[ "$deployed" == "$target" ]]; then
  echo "$target" >"$deployed_file"
  echo "auto-update: up to date (${target:0:7})"
  if [[ -f "$push_failed_file" ]] && push_image; then
    notify "Spot 학습용 이미지를 올렸습니다 (${target:0:7})."
  fi
  exit 0
fi
if [[ -f "$failed_file" && "$(cat "$failed_file")" == "$target" ]]; then
  echo "auto-update: ${target:0:7} did not start before; waiting for a newer main"
  exit 0
fi
changes=$(git log -n 10 --format='- %s' "$deployed..$target")
git merge -q --ff-only "$target"

if restart; then
  echo "$target" >"$deployed_file"
  rm -f "$failed_file"
  pushed=""
  if ! push_image; then
    pushed=$'\n'"Spot 학습용 이미지를 올리지 못했습니다. 밤 학습은 이전 이미지로 돌고, 다음 06:00에 다시 올립니다."
  fi
  docker image prune -f >/dev/null || true
  echo "auto-update: ${deployed:0:7} -> ${target:0:7}"
  notify "자동 업데이트: ${deployed:0:7} → ${target:0:7}"$'\n'"$changes$pushed"
  exit 0
fi

echo "auto-update: ${target:0:7} did not start; going back to ${deployed:0:7}" >&2
echo "$target" >"$failed_file"
git reset -q --hard "$deployed"
if restart; then
  state="${deployed:0:7}로 되돌렸습니다"
else
  state="${deployed:0:7}로 되돌렸지만 엔진이 응답하지 않습니다. 확인이 필요합니다"
fi
notify "자동 업데이트 실패: ${target:0:7}이 시작되지 않아 $state."
exit 1
