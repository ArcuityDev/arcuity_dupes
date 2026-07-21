#!/bin/bash
# Run this after the local scan finishes to push DB to Lambda.
# The watch_and_run.sh on Lambda will pick it up automatically.
set -euo pipefail
DB="$(cd "$(dirname "$0")/.." && pwd)/clear_local_dupes.db"
LAMBDA="ubuntu@170.9.56.76"
KEY="$HOME/.ssh/joe-05012026.pem"
REMOTE="/home/ubuntu/clear_local_dupes/"

echo "Waiting for scan to complete..."
while ! grep -q "SCAN_DONE" /tmp/cld_scan.log 2>/dev/null; do
  sleep 30
  echo "  Scan still running..."
done

echo "Scan done. Syncing DB to Lambda..."
rsync -az -e "ssh -i $KEY -o StrictHostKeyChecking=no" "$DB" "$LAMBDA:$REMOTE"
echo "DB synced. Lambda watcher will now run analysis automatically."
echo "Monitor: ssh -i $KEY $LAMBDA 'tail -f /home/ubuntu/clear_local_dupes/watch_and_run.log'"
