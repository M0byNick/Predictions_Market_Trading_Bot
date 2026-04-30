#!/usr/bin/env bash
# Install cron entries for the Arb_Bot daily pipeline + half-hourly collector.
#
# Usage:
#   bash scripts/install_cron.sh           # show what would be added
#   bash scripts/install_cron.sh --apply   # actually install via `crontab`
#
# Schedule (UTC):
#   03:00 daily       — submit batch (run_daily_pipeline.py)
#   05-23 every 30m   — poll/collect (run_collection.py)
#   :15 every hour    — refresh market data only (skip-batch, no LLM cost)

set -euo pipefail

ARB_BOT="/Users/nicholasmorihisa/Documents/2026/Professional/Trading/Prediction_Markets/.claude/worktrees/gifted-hopper-5e82df/Prediction_Markets/Arb_Bot"
PYTHON="$ARB_BOT/.venv/bin/python"
LOGDIR="$ARB_BOT/data/logs"

DAILY="0 3 * * * cd \"$ARB_BOT\" && \"$PYTHON\" \"$ARB_BOT/scripts/run_daily_pipeline.py\" >> \"$LOGDIR/cron_daily.out\" 2>&1"
COLLECT="*/30 5-23 * * * cd \"$ARB_BOT\" && \"$PYTHON\" \"$ARB_BOT/scripts/run_collection.py\" >> \"$LOGDIR/cron_collection.out\" 2>&1"
HOURLY_INGEST="15 * * * * cd \"$ARB_BOT\" && \"$PYTHON\" \"$ARB_BOT/scripts/run_daily_pipeline.py\" --skip-batch >> \"$LOGDIR/cron_hourly.out\" 2>&1"

echo "=== Proposed cron entries (Arb_Bot pipeline) ==="
echo
echo "# 03:00 UTC daily — full pipeline + LLM batch submission"
echo "$DAILY"
echo
echo "# Every 30 min from 05:00-23:59 UTC — poll/collect any in-flight batch"
echo "$COLLECT"
echo
echo "# Every hour at :15 — refresh market data only (no LLM cost)"
echo "$HOURLY_INGEST"
echo

if [[ "${1:-}" == "--apply" ]]; then
    mkdir -p "$LOGDIR"
    # Remove any prior arb_bot lines, then append new ones
    EXISTING="$(crontab -l 2>/dev/null | grep -v 'Arb_Bot/scripts/run_' || true)"
    NEW=$(printf "%s\n# Arb_Bot daily pipeline (auto-installed)\n%s\n%s\n%s\n" \
        "$EXISTING" "$DAILY" "$COLLECT" "$HOURLY_INGEST")
    echo "$NEW" | crontab -
    echo "INSTALLED. Verify with: crontab -l"
else
    echo "(dry run) Re-run with --apply to install via crontab."
    echo
    echo "After installing, monitor:"
    echo "  tail -f \"$LOGDIR/cron_daily.out\""
    echo "  tail -f \"$LOGDIR/daily_pipeline_\$(date -u +%Y%m%d).log\""
    echo
    echo "macOS note: cron jobs run only when your machine is awake. For"
    echo "a 24/7 schedule, deploy to the VPS (Bot Runner) and install"
    echo "there. Or use launchd with a KeepAlive plist for Mac wake."
fi
