#!/bin/bash
# Start the analysis pipeline on Lambda inside a tmux session.
# Usage: bash deploy/run_on_lambda.sh [--phase PHASE]
#
# Phases: fetch-github | analyze | recommend | report | full-analysis
# Default: full-analysis (all phases except local scan)

set -euo pipefail

LAMBDA_HOST="ubuntu@170.9.56.76"
SSH_KEY="$HOME/.ssh/joe-05012026.pem"
REMOTE_DIR="/home/ubuntu/clear_local_dupes"
PHASE="${1:-full-analysis}"
SESSION="cld_${PHASE}"

ssh_cmd() {
    ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$LAMBDA_HOST" "$@"
}

# Check for existing session
EXISTING=$(ssh_cmd "tmux ls 2>/dev/null | grep '^$SESSION' || true")
if [[ -n "$EXISTING" ]]; then
    echo "Session '$SESSION' already exists. Attaching..."
    ssh -tt -i "$SSH_KEY" -o StrictHostKeyChecking=no "$LAMBDA_HOST" "tmux attach -t $SESSION"
    exit 0
fi

GITHUB_TOKEN="${GITHUB_TOKEN:-}"
if [[ -z "$GITHUB_TOKEN" ]]; then
    echo "ERROR: GITHUB_TOKEN not set in environment."
    exit 1
fi

case "$PHASE" in
    fetch-github)
        CMD="python main.py fetch-github --token '$GITHUB_TOKEN'"
        ;;
    analyze)
        CMD="python main.py analyze"
        ;;
    recommend)
        CMD="python main.py recommend"
        ;;
    report)
        CMD="python main.py report"
        ;;
    scripts)
        CMD="python main.py scripts --token '$GITHUB_TOKEN'"
        ;;
    full-analysis)
        CMD="python main.py fetch-github --token '$GITHUB_TOKEN' && \
             python main.py analyze && \
             python main.py recommend && \
             python main.py report"
        ;;
    *)
        echo "Unknown phase: $PHASE"
        echo "Valid: fetch-github | analyze | recommend | report | scripts | full-analysis"
        exit 1
        ;;
esac

FULL_CMD="cd $REMOTE_DIR && source .venv/bin/activate && $CMD; echo '=== DONE ==='; read"

echo "=== Launching tmux session '$SESSION' on Lambda ==="
ssh_cmd "tmux new-session -d -s '$SESSION' \"bash -c '$FULL_CMD'\""

echo ""
echo "Session started. To watch:"
echo "  ssh -tt -i $SSH_KEY $LAMBDA_HOST 'tmux attach -t $SESSION'"
echo ""
echo "To detach without stopping: Ctrl-B then D"
echo "To check status without attaching:"
echo "  ssh -i $SSH_KEY $LAMBDA_HOST 'tmux capture-pane -t $SESSION -p | tail -30'"
