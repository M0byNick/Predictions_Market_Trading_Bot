"""
SQLite database layer for the Kalshi prediction market bot.

Replaces the JSON-based storage in tracker.py with SQLite for better querying,
aggregation, and crash safety. Uses Python's built-in sqlite3 module with
WAL mode for concurrent read access (dashboard, calibration, etc.).

Tables:
  - trades: Append-only trade audit trail
  - pending_orders: Crash recovery for in-flight orders
  - market_snapshots: Decision audit trail (migrated from JSONL)
  - daily_pnl: Aggregated daily metrics per category
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from log import logger


def init_db(db_path: str) -> sqlite3.Connection:
    """
    Initialize the SQLite database with WAL mode and create tables
    if they don't exist. Returns a connection.
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            category        TEXT NOT NULL,
            side            TEXT NOT NULL CHECK(side IN ('yes', 'no')),
            your_prob       REAL NOT NULL,
            market_prob     REAL NOT NULL,
            edge_at_entry   REAL NOT NULL,
            num_contracts   INTEGER NOT NULL,
            cost_usd        REAL NOT NULL,
            kelly_fraction  REAL NOT NULL,
            entry_time      TEXT NOT NULL,
            outcome         TEXT CHECK(outcome IN ('win', 'loss')),
            settlement_price REAL,
            pnl_usd         REAL,
            settlement_time TEXT,
            notes           TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS pending_orders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            side        TEXT NOT NULL,
            contracts   INTEGER NOT NULL,
            cost_usd    REAL NOT NULL,
            order_id    TEXT,
            timestamp   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS market_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            cycle_id    TEXT DEFAULT '',
            ticker      TEXT,
            category    TEXT,
            decision    TEXT,
            data        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_pnl (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            category        TEXT NOT NULL,
            realized_pnl    REAL NOT NULL DEFAULT 0,
            trade_count     INTEGER NOT NULL DEFAULT 0,
            win_count       INTEGER NOT NULL DEFAULT 0,
            loss_count      INTEGER NOT NULL DEFAULT 0,
            UNIQUE(date, category)
        );

        CREATE INDEX IF NOT EXISTS idx_trades_category ON trades(category);
        CREATE INDEX IF NOT EXISTS idx_trades_outcome ON trades(outcome);
        CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
        CREATE INDEX IF NOT EXISTS idx_pending_ticker_side ON pending_orders(ticker, side);
        CREATE INDEX IF NOT EXISTS idx_snapshots_cycle ON market_snapshots(cycle_id);
        CREATE INDEX IF NOT EXISTS idx_daily_pnl_date ON daily_pnl(date);
    """)

    conn.commit()
    logger.info("Database initialized at %s", db_path)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Context manager for atomic transactions."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def migrate_json_trades(conn: sqlite3.Connection, trades_file: str) -> int:
    """
    Import trades from the legacy JSON file into SQLite.
    Returns the number of trades imported. Skips if trades already exist.
    """
    import json

    existing = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    if existing > 0:
        logger.info("Database already has %d trades, skipping JSON import", existing)
        return 0

    if not os.path.exists(trades_file):
        return 0

    with open(trades_file, "r") as f:
        trades = json.load(f)

    if not trades:
        return 0

    count = 0
    with transaction(conn):
        for t in trades:
            conn.execute("""
                INSERT INTO trades (ticker, category, side, your_prob, market_prob,
                    edge_at_entry, num_contracts, cost_usd, kelly_fraction,
                    entry_time, outcome, settlement_price, pnl_usd,
                    settlement_time, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                t["ticker"], t["category"], t["side"],
                t["your_prob"], t["market_prob"], t["edge_at_entry"],
                t["num_contracts"], t["cost_usd"], t["kelly_fraction"],
                t["entry_time"], t.get("outcome"), t.get("settlement_price"),
                t.get("pnl_usd"), t.get("settlement_time"), t.get("notes", ""),
            ))
            count += 1

    logger.info("Migrated %d trades from %s to SQLite", count, trades_file)
    return count


def migrate_json_pending(conn: sqlite3.Connection, pending_file: str) -> int:
    """Import pending orders from the legacy JSON file into SQLite."""
    import json

    existing = conn.execute("SELECT COUNT(*) FROM pending_orders").fetchone()[0]
    if existing > 0:
        return 0

    if not os.path.exists(pending_file):
        return 0

    try:
        with open(pending_file, "r") as f:
            pending = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return 0

    count = 0
    with transaction(conn):
        for p in pending:
            conn.execute("""
                INSERT INTO pending_orders (ticker, side, contracts, cost_usd,
                    order_id, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                p["ticker"], p["side"], p["contracts"], p["cost_usd"],
                p.get("order_id"), p["timestamp"],
            ))
            count += 1

    logger.info("Migrated %d pending orders from %s to SQLite", count, pending_file)
    return count


def migrate_jsonl_snapshots(conn: sqlite3.Connection, snapshots_file: str) -> int:
    """Import snapshots from the legacy JSONL file into SQLite."""
    import json

    existing = conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
    if existing > 0:
        return 0

    if not os.path.exists(snapshots_file):
        return 0

    count = 0
    with transaction(conn):
        with open(snapshots_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    conn.execute("""
                        INSERT INTO market_snapshots (timestamp, cycle_id, ticker,
                            category, decision, data)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        record.get("timestamp", ""),
                        record.get("cycle_id", ""),
                        record.get("ticker"),
                        record.get("category"),
                        record.get("decision"),
                        json.dumps(record),
                    ))
                    count += 1
                except (json.JSONDecodeError, KeyError):
                    continue

    logger.info("Migrated %d snapshots from %s to SQLite", count, snapshots_file)
    return count
