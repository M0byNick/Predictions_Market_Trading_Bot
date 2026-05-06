#!/usr/bin/env bash
# Install the Polymarket WS market listener as a macOS launchd LaunchAgent
# so it auto-starts on login and auto-restarts on crash.
#
# Usage:
#   bash scripts/install_ws_listener_launchd.sh           # show plist + dry-run
#   bash scripts/install_ws_listener_launchd.sh --apply   # write plist, load it
#   bash scripts/install_ws_listener_launchd.sh --unload  # stop + uninstall
#
# Plist target: ~/Library/LaunchAgents/com.arb_bot.ws_listener.plist
# Logs:         data/logs/ws_listener.out  (plus .err for stderr)
#
# launchd will:
#   - run at user login (RunAtLoad=true)
#   - restart on exit (KeepAlive=true)
#   - throttle restarts (ThrottleInterval=10s)

set -euo pipefail

ARB_BOT="/Users/nicholasmorihisa/Documents/2026/Professional/Trading/Prediction_Markets/.claude/worktrees/gifted-hopper-5e82df/Prediction_Markets/Arb_Bot"
PYTHON="$ARB_BOT/.venv/bin/python"
SCRIPT="$ARB_BOT/scripts/ws_market_listener.py"
LOGDIR="$ARB_BOT/data/logs"
LABEL="com.arb_bot.ws_listener"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

PLIST_BODY=$(cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-u</string>
        <string>$SCRIPT</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$ARB_BOT</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>$LOGDIR/ws_listener.out</string>

    <key>StandardErrorPath</key>
    <string>$LOGDIR/ws_listener.err</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
EOF
)

case "${1:-}" in
    --apply)
        mkdir -p "$LOGDIR"
        mkdir -p "$(dirname "$PLIST")"
        echo "$PLIST_BODY" > "$PLIST"
        # bootstrap (modern) with fallback to load (legacy)
        if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
            echo "INSTALLED via bootstrap. Verify: launchctl print gui/\$(id -u)/$LABEL"
        else
            launchctl load "$PLIST"
            echo "INSTALLED via load (legacy). Verify: launchctl list | grep $LABEL"
        fi
        echo
        echo "Logs: $LOGDIR/ws_listener.{out,err}"
        echo "Tail: tail -f $LOGDIR/ws_listener.out"
        ;;
    --unload)
        if launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; then
            echo "STOPPED via bootout."
        else
            launchctl unload "$PLIST" 2>/dev/null || true
            echo "STOPPED via unload (legacy)."
        fi
        if [[ -f "$PLIST" ]]; then
            rm "$PLIST"
            echo "Plist removed: $PLIST"
        fi
        ;;
    *)
        echo "=== Proposed plist for $LABEL ==="
        echo
        echo "Location: $PLIST"
        echo
        echo "$PLIST_BODY"
        echo
        echo "(dry run) Re-run with --apply to install."
        echo "Stop + uninstall later: bash $0 --unload"
        echo
        echo "macOS note: launchd LaunchAgents only run while you are"
        echo "logged in. For 24/7 uptime, deploy to the Bot Runner VPS."
        ;;
esac
