#!/usr/bin/env bash
# Copies this repo to the server and runs the bootstrap there. Run it on YOUR computer:
#
#     bash scripts/deploy.sh ubuntu@80.225.248.25 ~/Downloads/ssh-key.key [hostname]
#
# or set GYAN_VM, GYAN_KEY and GYAN_HOST in the environment. Needs ssh and tar (Git Bash on
# Windows has both). Nothing secret is copied: .env is created on the server, never uploaded.
# Images are built on the server, so ARM or Intel does not matter.
set -euo pipefail

VM="${1:-${GYAN_VM:-}}"
KEY="${2:-${GYAN_KEY:-}}"
HOST="${3:-${GYAN_HOST:-}}"
[ -n "$VM" ] || { echo "usage: bash scripts/deploy.sh user@server [ssh-key] [hostname]"; exit 1; }

cd "$(dirname "$0")/.."
SSH=(ssh -o BatchMode=yes)
[ -n "$KEY" ] && SSH+=(-i "$KEY")

echo "== copying the repo to $VM:~/gyan"
tar --exclude='.git' --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='.ruff_cache' --exclude='*.egg-info' --exclude='.env' --exclude='data' \
    --exclude='ui/.local-test-account' --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    -czf - . | "${SSH[@]}" "$VM" 'mkdir -p ~/gyan && tar -xzf - -C ~/gyan'

echo "== running the bootstrap on the server"
"${SSH[@]}" "$VM" "cd ~/gyan && GYAN_HOST='$HOST' bash scripts/vm-bootstrap.sh"
