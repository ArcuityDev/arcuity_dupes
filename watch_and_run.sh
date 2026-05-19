#!/bin/bash
# Watches for local scan DB to arrive, then runs full analysis pipeline.
# Run as: nohup bash watch_and_run.sh > watch_and_run.log 2>&1 &

set -euo pipefail
PROJ="/home/ubuntu/clear_local_dupes"
DB="$PROJ/clear_local_dupes.db"
LOG="$PROJ/watch_and_run.log"

cd "$PROJ"
source .venv/bin/activate
set -a && source .env && set +a

echo "[$(date)] Watcher started. Waiting for $DB to arrive..."

# Wait for DB (rsynced from local after scan completes)
while [ ! -f "$DB" ]; do
  sleep 30
  echo "[$(date)] Still waiting for DB..."
done

echo "[$(date)] DB arrived. Waiting for github fetch to finish..."
# Wait for GitHub fetch PID to finish
if [ -f /tmp/cld_github.pid ]; then
  GPID=$(cat /tmp/cld_github.pid)
  while kill -0 "$GPID" 2>/dev/null; do
    sleep 15
    echo "[$(date)] GitHub fetch still running (pid $GPID)..."
  done
fi
echo "[$(date)] GitHub fetch done."

# Merge github.db into clear_local_dupes.db
echo "[$(date)] Merging github data into main DB..."
python3 -c "
import sqlite3
conn = sqlite3.connect('$DB')
conn.execute(\"ATTACH DATABASE '$PROJ/github.db' AS src\")
conn.execute(\"INSERT OR REPLACE INTO github_repos SELECT * FROM src.github_repos\")
conn.execute(\"INSERT OR IGNORE INTO github_files SELECT * FROM src.github_files\")
conn.commit()
conn.close()
print('Merge complete')
"

echo "[$(date)] Running analyze..."
python main.py analyze --db "$DB"

echo "[$(date)] Running recommend..."
python main.py recommend --db "$DB"

echo "[$(date)] Generating reports..."
mkdir -p "$PROJ/reports"
python main.py report --db "$DB" --output-dir "$PROJ/reports"

echo "[$(date)] ALL DONE. Reports in $PROJ/reports/"
