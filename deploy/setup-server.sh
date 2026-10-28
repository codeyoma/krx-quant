#!/usr/bin/env bash
# Prepare a fresh Lightsail Ubuntu 24.04 instance for the engine, then start it.
#
# Run on the server, from the repository checkout:
#
#   sudo deploy/setup-server.sh            # interactive Tailscale login
#   sudo TS_AUTHKEY=tskey-... deploy/setup-server.sh
#
# Safe to run again. On the first run it creates .env from .env.example and stops so
# you can fill it in; run it again afterwards. A rerun restarts neither Docker nor a running
# engine, so recording goes on (#385). See docs/operations.md.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "setup-server: run with sudo" >&2
  exit 1
fi
repo=$(cd "$(dirname "$0")/.." && pwd)
owner=${SUDO_USER:-ubuntu}
engine_uid=10001 # the image's engine user

set_env() {
  # Set KEY=value in .env, replacing an existing line.
  if grep -q "^$1=" .env; then
    sed -i "s|^$1=.*|$1=$2|" .env
  else
    printf '%s=%s\n' "$1" "$2" >>.env
  fi
}

echo "== packages"
# Only the missing ones: on a rerun apt-get would upgrade docker.io, and the upgrade restarts
# Docker and the engine with it (#385).
missing=()
for package in ca-certificates curl git ufw docker.io docker-compose-v2; do
  # shellcheck disable=SC2016 # dpkg-query's own field, not a shell variable
  if [[ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)" != installed ]]; then
    missing+=("$package")
  fi
done
if ((${#missing[@]})); then
  apt-get update -q
  apt-get install -y -q "${missing[@]}"
fi
usermod -aG docker "$owner"

echo "== swap"
# The nightly training runs beside the engine on a 2 GB server; without swap the kernel's OOM
# killer ended it (2026-10-22, #355). Made once and kept across reboots; a server that already
# has swap on (any file or partition) is left alone, /etc/fstab included.
if [[ -z "$(swapon --show --noheadings)" ]]; then
  if [[ ! -f /swapfile ]]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
fi

echo "== kernel self-reboot and a journal on disk (#385)"
"$repo/deploy/kernel-recovery.sh"

echo "== Tailscale"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg \
    -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list \
    -o /etc/apt/sources.list.d/tailscale.list
  apt-get update -q
  apt-get install -y -q tailscale
fi
if [[ -n "${TS_AUTHKEY:-}" ]]; then
  tailscale up --ssh --hostname=krx-quant --auth-key="$TS_AUTHKEY"
else
  tailscale up --ssh --hostname=krx-quant
fi

echo "== Docker starts after Tailscale"
# The engine port is bound to the Tailscale address. At boot Docker must not try to bind
# it before tailscaled has brought the address up, so order the services and allow
# binding an address that is not up yet. Docker is restarted only when this changes, so
# a rerun does not interrupt recording.
dropin=/etc/systemd/system/docker.service.d/after-tailscale.conf
if [[ ! -f "$dropin" ]]; then
  mkdir -p "$(dirname "$dropin")"
  printf '[Unit]\nAfter=tailscaled.service\nWants=tailscaled.service\n' >"$dropin"
  echo 'net.ipv4.ip_nonlocal_bind = 1' >/etc/sysctl.d/90-krx-quant.conf
  sysctl -q --system
  systemctl daemon-reload
  systemctl restart docker
fi

echo "== firewall"
if ! ufw status | grep -q "Status: active"; then
  # First setup only: a rerun must not reopen port 22 after it was closed.
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow 22/tcp # Lightsail browser SSH; close it once Tailscale SSH works
fi
ufw allow in on tailscale0
ufw allow 41641/udp # Tailscale direct connections
ufw --force enable

echo "== engine settings"
cd "$repo"
if [[ ! -f .env ]]; then
  cp .env.example .env
  chown "$owner": .env
  deploy/secure-env.sh .env
  echo "Fill in $repo/.env (docs/operations.md), then run this script again."
  exit 1
fi
set_env KRX_BIND_ADDRESS "$(tailscale ip -4 | head -n1)"
chown "$owner": .env
deploy/secure-env.sh .env
mkdir -p data state
chown -R "$engine_uid:$engine_uid" data state

echo "== start"
# A rerun leaves a running engine alone: built again by hand (KRX_COMMIT unknown) its image
# differs from the 06:00 update's, and compose would recreate it, cutting the recording (#385).
# After changing .env, run docker compose up -d.
if [[ -n "$(docker compose ps -q --status running engine 2>/dev/null || true)" ]]; then
  echo "the engine is running: left as it is"
else
  docker compose up -d --build
fi
docker compose ps

echo "== daily update"
deploy/install-auto-update.sh
port=$(grep '^KRX_ENGINE_PORT=' .env | cut -d= -f2)
echo "Engine API: http://$(grep '^KRX_BIND_ADDRESS=' .env | cut -d= -f2):${port:-8080} (Tailscale only)"
