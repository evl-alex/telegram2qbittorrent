#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

git fetch --quiet
if [ "$(git rev-parse HEAD)" = "$(git rev-parse @{u})" ]; then
    echo "Already up to date."
    exit 0
fi

git pull --ff-only

if git diff --name-only HEAD@{1} HEAD | grep -qx requirements.txt; then
    ./venv/bin/pip install -r requirements.txt
fi

sudo systemctl restart telegram2qbittorrent
echo "Updated and restarted."
