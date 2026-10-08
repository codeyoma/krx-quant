#!/usr/bin/env bash
# Keep the engine's .env readable by its owner only: it holds the KIS app secret, the
# Telegram bot token and the AWS keys.
#
# Usage: deploy/secure-env.sh [path/to/.env]   (default: .env at the repository root)
set -euo pipefail

env_file=${1:-"$(cd "$(dirname "$0")/.." && pwd)/.env"}
if [[ ! -f "$env_file" ]]; then
  echo "secure-env: $env_file not found; copy .env.example to .env and fill it in" >&2
  exit 1
fi
chmod 600 "$env_file"
mode=$(stat -c %a "$env_file" 2>/dev/null || stat -f %Lp "$env_file")
if [[ "$mode" != "600" ]]; then
  echo "secure-env: $env_file is mode $mode, not 600" >&2
  exit 1
fi
echo "secure-env: $env_file is mode 600"
