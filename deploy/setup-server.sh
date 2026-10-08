#!/usr/bin/env bash
# Prepare a fresh Lightsail Ubuntu 24.04 instance for the engine, then start it.
#
# Run on the server, from the repository checkout:
#
#   sudo deploy/setup-server.sh            # interactive Tailscale login
#   sudo TS_AUTHKEY=tskey-... deploy/setup-server.sh
#
# Safe to run again. On the first run it creates .env from .env.example and stops so
# you can fill it in; run it again afterwards. See docs/operations.md.
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
apt-get update -q
apt-get install -y -q ca-certificates curl git ufw docker.io docker-compose-v2
usermod -aG docker "$owner"

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
docker compose up -d --build
docker compose ps
echo "Engine port: $(grep '^KRX_BIND_ADDRESS=' .env | cut -d= -f2) (Tailscale only)"
