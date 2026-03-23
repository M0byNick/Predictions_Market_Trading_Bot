"""
Market snapshot logger — append-only JSONL for audit trail.

Every market evaluated by a screener gets a snapshot recording why it was
traded, skipped, or rejected. This creates a decision audit trail for
post-hoc analysis and model calibration.

Format: one JSON object per line in data/snapshots.jsonl (crash-safe appending).
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone

SNAPSHOTS_FILE = os.path.join("data", "snapshots.jsonl")


def log_snapshot(data: dict, cycle_id: str = None) -> None:
    """
    Append a market snapshot to the JSONL file.

    Args:
        data: Snapshot dict from a screener (ticker, model_prob, decision, etc.)
        cycle_id: UTC timestamp grouping snapshots from the same screening cycle.
    """
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cycle_id": cycle_id or "",
        **data,
    }
    os.makedirs(os.path.dirname(SNAPSHOTS_FILE), exist_ok=True)
    with open(SNAPSHOTS_FILE, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
