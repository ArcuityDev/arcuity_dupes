#!/bin/bash
# Sync project code AND the SQLite DB to Lambda.
# Usage: bash deploy/sync_to_lambda.sh [--db-only]
#
# Run from project root:  bash deploy/sync_to_lambda.sh

set -euo pipefail

LAMBDA_HOST="ubuntu@170.9.56.76"
SSH_KEY="$HOME/.ssh/joe-05012026.pem"
REMOTE_DIR="/home/ubuntu/clear_local_dupes"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

DB_ONLY="${1:-}"

ssh_cmd() {
    ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$LAMBDA_HOST" "$@"
}

rsync_cmd() {
    rsync -avz --progress -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" "$@"
}

ssh_cmd "mkdir -p $REMOTE_DIR"

if [[ "$DB_ONLY" == "--db-only" ]]; then
    echo "=== Syncing DB only ==="
    DB_FILE=$(ls "$LOCAL_DIR"/*.db 2>/dev/null | head -1 || true)
    if [[ -z "$DB_FILE" ]]; then
        echo "ERROR: no .db file found in $LOCAL_DIR"
        exit 1
    fi
    rsync_cmd "$DB_FILE" "$LAMBDA_HOST:$REMOTE_DIR/"
    echo "DB synced: $(basename "$DB_FILE")"
else
    echo "=== Syncing full project ==="
    rsync_cmd \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.venv' \
        --exclude='reports/' \
        --exclude='consolidation_scripts/' \
        "$LOCAL_DIR/" "$LAMBDA_HOST:$REMOTE_DIR/"

    # Sync DB if it exists
    DB_FILE=$(ls "$LOCAL_DIR"/*.db 2>/dev/null | head -1 || true)
    if [[ -n "$DB_FILE" ]]; then
        rsync_cmd "$DB_FILE" "$LAMBDA_HOST:$REMOTE_DIR/"
        echo "DB also synced: $(basename "$DB_FILE")"
    fi

    echo "=== Installing dependencies on Lambda ==="
    ssh_cmd "cd $REMOTE_DIR && python3 -m venv .venv && source .venv/bin/activate && pip install -q -r requirements.txt"
fi

echo ""
echo "=== Sync complete ==="
echo "  SSH in:  ssh -i $SSH_KEY $LAMBDA_HOST"
echo "  Run:     cd $REMOTE_DIR && source .venv/bin/activate && python main.py status"
