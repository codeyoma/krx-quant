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
# 3. It deploys that commit only if the ci workflow passed on it (#541): the run that its push
#    to main started, as GitHub's API lists it. A commit that only moves the fincept pointer
#    has no run, and the commit it was made on answers for it. While that run waits or runs, if
#    it failed, or if GitHub cannot be read, the engine keeps running what it ran and the
#    journal names the commit and why. A commit whose ci failed is told on Telegram once
#    (.auto-update-ci-failed); a GitHub that cannot be read, on every run it is so.
# 4. It fast-forwards, rebuilds and restarts the engine.
# 5. It waits for the Engine API to answer (3 minutes). If it does not, it goes back to
#    the commit before, rebuilds, and remembers the commit that failed.
# 6. It pushes the new image to ECR for the nightly training on EC2 Spot, if that is set
#    up (deploy/push-image.sh, #23). A push that failed is tried again on the next run,
#    even with nothing new on main (.auto-update-push-failed).
# 7. It removes Docker's build cache and the images not used for 3 days (#379), and reports
#    to Telegram (krx-quant notify), with what Docker keeps and the disk's free space.
#
# Every run, before all that, it applies the kernel's self-reboot settings and the journal on
# disk (deploy/kernel-recovery.sh, #385), so they reach the server without anyone running
# setup-server.sh (#389).
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"
tries=${KRX_UPDATE_TRIES:-36}
pause=${KRX_UPDATE_PAUSE:-5}
failed_file=.auto-update-failed
deployed_file=.auto-update-deployed
push_failed_file=.auto-update-push-failed
ci_failed_file=.auto-update-ci-failed
github_api=${KRX_UPDATE_GITHUB_API:-https://api.github.com/repos/codeyoma/krx-quant}

read -r weekday hhmm <<<"${KRX_UPDATE_NOW:-$(TZ=Asia/Seoul date '+%u %H%M')}"
if ((weekday <= 5 && 10#$hhmm >= 750 && 10#$hhmm < 2010)); then
  echo "auto-update: market hours (weekdays 07:50-20:10 KST); not updating" >&2
  exit 1
fi

# A notice on Telegram; fails when it did not go.
send() {
  docker compose run --rm --no-deps engine notify "$1" >/dev/null 2>&1 || {
    echo "auto-update: could not send to Telegram: $1" >&2
    return 1
  }
}

notify() {
  send "$1" || true
}

# The ci workflow's result for exactly this commit of main (#541), from the run its push
# started, as one line "<state>\t<what>\t<the run's page>": success; failure and the conclusion;
# pending and the run's status, or "no run yet"; unreachable and why. GitHub's API without a
# token: the repository is public, and a run asks once, of the 60 an hour it allows. The host's
# python3 (Ubuntu's own) reads the JSON. GitHub's error text is left out: a rate limit's names
# the server's address.
ci_of() {
  python3 - "$github_api" "$1" <<'EOF'
import json
import sys
import urllib.error
import urllib.request

api, sha = sys.argv[1], sys.argv[2]
request = urllib.request.Request(
    api + "/actions/workflows/ci.yml/runs?event=push&head_sha=" + sha,
    headers={"Accept": "application/vnd.github+json", "User-Agent": "krx-quant-auto-update"},
)
try:
    with urllib.request.urlopen(request, timeout=30) as answer:
        listed = json.load(answer)["workflow_runs"]
    # This commit's push runs only: a pull request's run tests its merge with main, not this.
    runs = [run for run in listed if run["head_sha"] == sha and run["event"] == "push"]
    if not runs:
        print("pending\tno run yet\t")
    else:
        run = max(runs, key=lambda run: run["id"])  # the newest, if it was pushed twice
        if run["status"] != "completed":
            print("pending\t%s\t%s" % (run["status"], run["html_url"]))
        elif run["conclusion"] == "success":
            print("success\tsuccess\t%s" % run["html_url"])
        else:
            print("failure\t%s\t%s" % (run["conclusion"], run["html_url"]))
except urllib.error.HTTPError as error:
    left = error.headers.get("x-ratelimit-remaining")
    print("unreachable\tHTTP %d%s\t" % (error.code, "" if left is None else ", %s requests left this hour" % left))
except urllib.error.URLError as error:
    print("unreachable\tURLError(%s)\t" % type(error.reason).__name__)
except Exception as error:  # a timeout reading, an answer not in this shape
    print("unreachable\t%s\t" % type(error).__name__)
EOF
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

# The build cache and the images unused for 3 days (#379), after an update that started. The
# running engine's image is in use, and so kept; a rollback builds the commit before again
# (from the cache of the last 3 days when it has it), so it needs no old image. A prune that
# fails leaves the update as it is.
prune() {
  docker builder prune -af --filter until=72h >/dev/null ||
    echo "auto-update: could not prune the build cache" >&2
  docker image prune -af --filter until=72h >/dev/null ||
    echo "auto-update: could not prune the images" >&2
}

# What Docker keeps and the disk's free space, for the log and the notice: "<images> <build
# cache> <free> <size>", "?" for what could not be read.
usage() {
  local kept images cache free size
  kept=$(docker system df --format '{{.Type}}|{{.Size}}' 2>/dev/null) || kept=""
  images=$(awk -F'|' '$1 == "Images" {print $2}' <<<"$kept")
  cache=$(awk -F'|' '$1 == "Build Cache" {print $2}' <<<"$kept")
  read -r free size < <(df -Ph . 2>/dev/null | awk 'NR == 2 {print $4, $2}') || true
  echo "${images:-?} ${cache:-?} ${free:-?} ${size:-?}"
}

# The kernel's self-reboot settings (#385, #389). They need root: the owner's passwordless sudo
# (Lightsail's ubuntu has it); sudo -n never waits for a password. The script restarts neither
# Docker nor the engine and is the same when run again, so it runs every day, main moved or not.
# When it cannot run, the update goes on; krx-quant doctor section 6 shows the values.
if ! sudo -n "$repo/deploy/kernel-recovery.sh"; then
  echo "auto-update: could not apply deploy/kernel-recovery.sh (it needs passwordless sudo);" \
    "run sudo deploy/kernel-recovery.sh by hand" >&2
fi

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

# The commit whose ci answers for main (#541): main's own, or, while main's newest commits only
# move the fincept pointer, the one before them. The fincept-pointer workflow pushes those with
# GITHUB_TOKEN, which starts no workflow, so GitHub lists no run for them; the image and CI read
# nothing of fincept/ (.dockerignore, a checkout without submodules), so they run what that
# commit ran.
tested=$target
while [[ "$(git diff --name-only "$tested^" "$tested" 2>/dev/null || true)" == fincept ]]; do
  tested=$(git rev-parse "$tested^")
done
if [[ "$tested" != "$target" ]]; then
  echo "auto-update: main ${target:0:7} only moves the fincept pointer since ${tested:0:7}; its ci answers"
fi
result=$(ci_of "$tested") || result=$'unreachable\tpython3 did not run\t'
IFS=$'\t' read -r ci what run_page <<<"$result"
case "$ci" in
success)
  rm -f "$ci_failed_file"
  ;;
failure)
  echo "auto-update: keeping ${deployed:0:7}: the ci of $tested failed ($what) $run_page"
  # Told once a commit: a notice that did not go is sent again on the next run.
  if [[ "$(cat "$ci_failed_file" 2>/dev/null || true)" == "$tested" ]]; then
    echo "auto-update: told on Telegram before; waiting for a main whose ci passed"
  elif send "자동 업데이트 보류: main ${tested:0:7}의 CI가 실패했습니다($what). 엔진은 ${deployed:0:7} 그대로이고, CI가 통과한 main을 기다립니다."$'\n'"$run_page"; then
    echo "$tested" >"$ci_failed_file"
  fi
  exit 0
  ;;
pending)
  echo "auto-update: keeping ${deployed:0:7}: the ci of $tested is not done ($what) $run_page"
  exit 0
  ;;
*)
  echo "auto-update: keeping ${deployed:0:7}: could not read the ci of $tested from GitHub ($what)" >&2
  notify "자동 업데이트 보류: GitHub에서 main ${tested:0:7}의 CI 결과를 읽지 못했습니다($what). 엔진은 ${deployed:0:7} 그대로입니다."
  exit 1
  ;;
esac

changes=$(git log -n 10 --format='- %s' "$deployed..$target")
git merge -q --ff-only "$target"

if restart; then
  echo "$target" >"$deployed_file"
  rm -f "$failed_file"
  pushed=""
  if ! push_image; then
    pushed=$'\n'"Spot 학습용 이미지를 올리지 못했습니다. 밤 학습은 이전 이미지로 돌고, 다음 06:00에 다시 올립니다."
  fi
  prune
  read -r images cache free size < <(usage)
  echo "auto-update: ${deployed:0:7} -> ${target:0:7}"
  echo "auto-update: docker images $images, build cache $cache; disk free $free of $size"
  disk="디스크 여유 $free / $size · Docker 이미지 $images, 빌드 캐시 $cache"
  notify "자동 업데이트: ${deployed:0:7} → ${target:0:7}"$'\n'"$changes$pushed"$'\n'"$disk"
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
