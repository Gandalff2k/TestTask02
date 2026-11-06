#!/usr/bin/env bash
# One-time server setup on Ubuntu: swap, Docker, repository checkout, .env skeleton.
set -euo pipefail

REPO_URL="${1:?usage: bootstrap.sh <git repo url>}"
APP_DIR="${APP_DIR:-$HOME/app}"

if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

if ! command -v docker >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y docker.io docker-compose-v2 docker-buildx git curl
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER"
fi

if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
fi

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "Created $APP_DIR/.env from the example: fill in secrets, APP_HOST and MOCK_HOST."
fi

docker --version
docker compose version
