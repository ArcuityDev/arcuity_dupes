#!/bin/bash
# Run this ON the Lambda machine once to set up the environment.
# Usage: bash setup_lambda.sh

set -euo pipefail
PROJ_DIR="/home/ubuntu/clear_local_dupes"

echo "=== Setting up clear_local_dupes on Lambda ==="

sudo apt-get update -qq
sudo apt-get install -y python3-pip python3-venv git rsync

mkdir -p "$PROJ_DIR"
cd "$PROJ_DIR"

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip -q
pip install -r requirements.txt -q

echo ""
echo "=== Setup complete ==="
echo "  Project: $PROJ_DIR"
echo "  Activate: source $PROJ_DIR/.venv/bin/activate"
echo "  Run: python main.py --help"
