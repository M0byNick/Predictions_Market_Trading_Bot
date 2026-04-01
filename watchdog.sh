#!/bin/bash
# Watchdog for Kalshi paper trading bot
# Restarts main.py if it exits. Logs restarts.
# Usage: nohup ./watchdog.sh &

cd "$(dirname "$0")"
LOG="data/watchdog.log"

while true; do
    echo "$(date '+%Y-%m-%d %H:%M:%S') Starting main.py" >> "$LOG"
    /opt/anaconda3/bin/python main.py >> data/bot_run.log 2>&1
    EXIT_CODE=$?
    echo "$(date '+%Y-%m-%d %H:%M:%S') main.py exited with code $EXIT_CODE — restarting in 30s" >> "$LOG"
    sleep 30
done
