#!/usr/bin/env bash
# Push the engine's image to ECR for the nightly training on EC2 Spot (#23): the Spot run
# pulls krx-quant:latest from there (deploy/training.yaml).
#
# On the server, from anywhere: deploy/push-image.sh
# deploy/auto-update.sh runs it after each update. Without KRX_IMAGE_REPOSITORY in .env
# (the training stays on the server) it does nothing.
#
# The registry password comes from the engine's own AWS keys (krx-quant registry-password);
# docker forgets it again once the push is done.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"

repository=$(sed -n 's/^KRX_IMAGE_REPOSITORY=//p' .env 2>/dev/null | tail -n 1 | tr -d "\"'")
if [[ -z "$repository" ]]; then
  echo "push-image: KRX_IMAGE_REPOSITORY is not set in .env; nothing to push"
  exit 0
fi
registry=${repository%%/*}

docker compose run --rm --no-deps -T engine registry-password |
  docker login --username AWS --password-stdin "$registry" >/dev/null
trap 'docker logout "$registry" >/dev/null 2>&1 || true' EXIT
docker tag krx-quant:latest "$repository:latest"
docker push -q "$repository:latest" >/dev/null
echo "push-image: krx-quant:latest -> $repository:latest"
