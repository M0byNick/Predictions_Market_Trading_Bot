import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    venue TEXT NOT NULL,
    venue_market_id TEXT NOT NULL,
    title TEXT,
    description TEXT,
    resolution_criteria TEXT,
    resolution_source TEXT,
    close_time INTEGER,
    resolution_time INTEGER,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    volume REAL,
    liquidity REAL,
    first_seen_ts INTEGER NOT NULL,
    last_seen_ts INTEGER NOT NULL,
    status TEXT,
    raw_json TEXT,
    PRIMARY KEY (venue, venue_market_id)
);

CREATE INDEX IF NOT EXISTS idx_markets_status ON markets(status);
CREATE INDEX IF NOT EXISTS idx_markets_close_time ON markets(close_time);

CREATE TABLE IF NOT EXISTS market_embeddings (
    venue TEXT NOT NULL,
    venue_market_id TEXT NOT NULL,
    model TEXT NOT NULL,
    embedding BLOB NOT NULL,
    computed_ts INTEGER NOT NULL,
    PRIMARY KEY (venue, venue_market_id, model)
);

CREATE TABLE IF NOT EXISTS candidate_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kalshi_ticker TEXT NOT NULL,
    poly_us_market_id TEXT NOT NULL,
    cosine_similarity REAL NOT NULL,
    generated_ts INTEGER NOT NULL,
    UNIQUE (kalshi_ticker, poly_us_market_id)
);

CREATE INDEX IF NOT EXISTS idx_candidate_pairs_similarity ON candidate_pairs(cosine_similarity);

CREATE TABLE IF NOT EXISTS pair_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL,
    match TEXT NOT NULL,
    confidence REAL NOT NULL,
    resolution_aligned TEXT NOT NULL,
    resolution_divergence_risk TEXT NOT NULL,
    divergence_reason TEXT,
    normalized_question TEXT,
    reasoning TEXT,
    model TEXT NOT NULL,
    verdict_ts INTEGER NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES candidate_pairs(id)
);

CREATE INDEX IF NOT EXISTS idx_pair_verdicts_candidate ON pair_verdicts(candidate_id);

CREATE TABLE IF NOT EXISTS approved_pairs (
    pair_id TEXT PRIMARY KEY,
    kalshi_ticker TEXT NOT NULL,
    poly_us_market_id TEXT NOT NULL,
    normalized_question TEXT,
    resolution_divergence_risk TEXT NOT NULL,
    tag TEXT NOT NULL,
    approved_by TEXT,
    approved_ts INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    UNIQUE (kalshi_ticker, poly_us_market_id)
);

CREATE INDEX IF NOT EXISTS idx_approved_pairs_active ON approved_pairs(active, tag);

CREATE TABLE IF NOT EXISTS rejected_pairs (
    candidate_id INTEGER PRIMARY KEY,
    rejected_by TEXT,
    rejected_ts INTEGER NOT NULL,
    reason TEXT,
    FOREIGN KEY (candidate_id) REFERENCES candidate_pairs(id)
);

CREATE TABLE IF NOT EXISTS paper_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id TEXT NOT NULL,
    detected_ts INTEGER NOT NULL,
    kalshi_yes_mid REAL,
    poly_yes_mid REAL,
    raw_spread REAL,
    fee_adjusted_edge_bps REAL,
    direction TEXT,
    size_units INTEGER,
    would_trade INTEGER NOT NULL,
    reject_reason TEXT,
    FOREIGN KEY (pair_id) REFERENCES approved_pairs(pair_id)
);

CREATE INDEX IF NOT EXISTS idx_paper_signals_detected_ts ON paper_signals(detected_ts);
CREATE INDEX IF NOT EXISTS idx_paper_signals_pair ON paper_signals(pair_id);

CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    pair_id TEXT NOT NULL,
    leg TEXT NOT NULL,
    side TEXT NOT NULL,
    contract TEXT NOT NULL,
    price_intended REAL NOT NULL,
    price_filled REAL,
    size_filled INTEGER,
    fees_usd REAL,
    ts INTEGER NOT NULL,
    state TEXT NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES paper_signals(id)
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue TEXT NOT NULL,
    started_ts INTEGER NOT NULL,
    finished_ts INTEGER,
    markets_upserted INTEGER,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingestion_runs_venue_ts ON ingestion_runs(venue, started_ts);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
