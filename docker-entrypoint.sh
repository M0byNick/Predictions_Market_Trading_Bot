#!/usr/bin/env bash
set -euo pipefail

mkdir -p "${DATA_DIR:-/app/data}"

MODE="${ARB_BOT_ROLE:-runloop}"

case "$MODE" in
  runloop)
    exec python -u -m arb_bot.main
    ;;
  dashboard)
    exec python -u -m arb_bot.dashboard.app
    ;;
  seed)
    exec python -u scripts/seed_candidates.py
    ;;
  *)
    echo "Unknown ARB_BOT_ROLE: $MODE" >&2
    exit 1
    ;;
esac
