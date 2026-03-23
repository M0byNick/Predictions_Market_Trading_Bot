"""
Market snapshot logger — records decisions to SQLite for audit trail.

Every market evaluated by a screener gets a snapshot recording why it was
traded, skipped, or rejected. This creates a decision audit trail for
post-hoc analysis and model calibration.

Legacy: Previously wrote to data/snapshots.jsonl (JSONL, append-only).
Now writes to the SQLite market_snapshots table.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from db import init_db
import config
from log import logger

# Module-level connection (lazy init)
_conn = None


def _get_conn():
    global _conn
    if _conn is None:
        _conn = init_db(config.DB_PATH)
    return _conn


def log_snapshot(data: dict, cycle_id: str = None) -> None:
    """
    Record a market snapshot to the database.

    Args:
        data: Snapshot dict from a screener (ticker, model_prob, decision, etc.)
        cycle_id: UTC timestamp grouping snapshots from the same screening cycle.
    """
    conn = _get_conn()
    timestamp = datetime.now(timezone.utc).isoformat()

    conn.execute("""
        INSERT INTO market_snapshots (timestamp, cycle_id, ticker, category,
            decision, data)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        timestamp,
        cycle_id or "",
        data.get("ticker"),
        data.get("category"),
        data.get("decision"),
        json.dumps({**data, "timestamp": timestamp, "cycle_id": cycle_id or ""}, default=str),
    ))
    conn.commit()
