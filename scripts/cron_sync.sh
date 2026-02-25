#!/usr/bin/env bash
# Cron job: ensure API is running, then load tasks from Joplin.
#
# Install:
#   crontab -e
#   0 20 * * * /home/hamza/Projects/todo-tracker/scripts/cron_sync.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."

eval "$(/home/hamza/.local/share/miniforge/bin/mamba shell hook --shell bash)"
mamba activate todo

API_HOST=100.64.144.22
API_PORT=8001
API_URL="http://${API_HOST}:${API_PORT}"

# ── Ensure API server is running ──
if curl -sf "${API_URL}/tasks" > /dev/null 2>&1; then
    echo "API already running"
else
    echo "API not running, starting on ${API_HOST}:${API_PORT}..."
    python -m todo serve --host "$API_HOST" --port "$API_PORT" start >> data/api.log 2>&1 </dev/null &
    SERVER_PID=$!
    disown "$SERVER_PID"
    for i in $(seq 1 15); do
        sleep 1
        if curl -sf "${API_URL}/tasks" > /dev/null 2>&1; then
            echo "API started (pid $SERVER_PID)"
            break
        fi
    done
    if ! curl -sf "${API_URL}/tasks" > /dev/null 2>&1; then
        echo "WARNING: API failed to start within 15s, check data/api.log"
    fi
fi

# ── Load tasks from Joplin ──
python -m todo run load

echo "Done"
