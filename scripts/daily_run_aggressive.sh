#!/bin/bash
set -e

PROJECT_DIR="/Users/thunderopsai/Documents/Workspace/01_Projects/stock-exchange"
cd "$PROJECT_DIR"

# Ensure log directory exists
mkdir -p "$PROJECT_DIR/logs"

LOG_FILE="$PROJECT_DIR/logs/cron_aggressive_$(date +\%Y\%m\%d).log"

echo "=== [$(date '+%Y-%m-%d %H:%M:%S')] Starting Aggressive Trading Desk Run ===" >> "$LOG_FILE"
"$PROJECT_DIR/.venv/bin/python" -m src.main --profile aggressive --run-once >> "$LOG_FILE" 2>&1
EXIT_CODE=$?
echo "=== [$(date '+%Y-%m-%d %H:%M:%S')] Completed Aggressive Trading Desk Run (exit code: $EXIT_CODE) ===" >> "$LOG_FILE"
