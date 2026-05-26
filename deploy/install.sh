#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user (not root). It will use sudo when needed." >&2
    exit 1
fi

cd "$(dirname "$0")/.."
REPO="$(pwd)"
USER_NAME="$(id -un)"

echo "Installing telegram2qbittorrent for user '$USER_NAME' at $REPO"

sudo apt update
sudo apt install -y git python3 python3-venv

if [ ! -d venv ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

SCAFFOLDED=0
if [ ! -f .env ]; then
    cp .env.example .env
    echo ".env created from template."
    SCAFFOLDED=1
fi
if [ ! -f save_paths.json ]; then
    cp save_paths.example.json save_paths.json
    echo "save_paths.json created from template."
    SCAFFOLDED=1
fi
if [ "$SCAFFOLDED" -eq 1 ]; then
    echo
    echo "Edit the scaffolded config file(s), then re-run this script:"
    echo "  nano $REPO/.env"
    echo "  nano $REPO/save_paths.json"
    exit 0
fi

if grep -qE '^(BOT_TOKEN|ALLOWED_USER_IDS|QB_HOST|QB_PORT|QB_USER|QB_PASS)=$' .env; then
    echo
    echo "Some required values in .env are still empty. Fill them in, then re-run." >&2
    exit 1
fi

TMP_UNIT="$(mktemp)"
trap 'rm -f "$TMP_UNIT"' EXIT
sed \
    -e "s|__USER__|${USER_NAME}|g" \
    -e "s|__REPO__|${REPO}|g" \
    deploy/telegram2qbittorrent.service > "$TMP_UNIT"

if grep -q '__USER__\|__REPO__' "$TMP_UNIT"; then
    echo "Failed to substitute placeholders in unit file." >&2
    exit 1
fi

sudo install -m 644 "$TMP_UNIT" /etc/systemd/system/telegram2qbittorrent.service

sudo systemctl daemon-reload
sudo systemctl enable telegram2qbittorrent
sudo systemctl restart telegram2qbittorrent
sudo systemctl status telegram2qbittorrent --no-pager
