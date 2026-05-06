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
    poly_global_market_id TEXT NOT NULL,
    cosine_similarity REAL NOT NULL,
    generated_ts INTEGER NOT NULL,
    UNIQUE (kalshi_ticker, poly_global_market_id)
);

CREATE INDEX IF NOT EXISTS idx_candidate_pairs_similarity ON candidate_pairs(cosine_similarity);

CREATE TABLE IF NOT EXISTS pair_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL,
    match TEXT NOT NULL,
    confidence REAL NOT NULL,
    resolution_aligned TEXT NOT NULL,
    resolution_divergence_risk TEXT NOT NULL,
    match_polarity TEXT NOT NULL DEFAULT 'unknown',
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
    poly_global_market_id TEXT NOT NULL,
    normalized_question TEXT,
    resolution_divergence_risk TEXT NOT NULL,
    match_polarity TEXT NOT NULL DEFAULT 'unknown',
    tag TEXT NOT NULL,
    approved_by TEXT,
    approved_ts INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    -- Decision-context snapshot: pin the verdict the reviewer saw at the
    -- moment of approval, plus a JSON blob of the relevant fields. Lets
    -- /learn analyze "what attributes correlate with approval?" without
    -- being confused by later re-adjudications.
    verdict_id INTEGER,
    decision_context TEXT,  -- JSON: {model, match, confidence, risk, polarity, edge_case_flags, cosine}
    UNIQUE (kalshi_ticker, poly_global_market_id)
);

CREATE INDEX IF NOT EXISTS idx_approved_pairs_active ON approved_pairs(active, tag);

CREATE TABLE IF NOT EXISTS rejected_pairs (
    candidate_id INTEGER PRIMARY KEY,
    rejected_by TEXT,
    rejected_ts INTEGER NOT NULL,
    reason TEXT,
    -- Decision-context snapshot, mirrored from approved_pairs. Lets /learn
    -- compute approval rates by attribute (e.g., "you reject 95% of
    -- chamber_control pairs but approve 90% of game_void_rules").
    verdict_id INTEGER,
    decision_context TEXT,
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
    -- Settlement (filled in by scripts/settle_paper_fills.py once the
    -- underlying market resolves). NULL while the position is open.
    realized_outcome TEXT,         -- 'yes' | 'no' | 'void'
    realized_pnl_usd REAL,         -- net of fees, this leg only
    settled_ts INTEGER,
    settle_method TEXT,            -- 'price_extreme' | 'venue_status' | 'manual'
    FOREIGN KEY (signal_id) REFERENCES paper_signals(id)
);
-- (idx_paper_fills_realized + idx_paper_fills_pair created post-ALTER in
-- init_schema, since older DBs may need the column added before the index.)

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
        # Idempotent upgrades for existing databases
        verdict_cols = {r["name"] for r in conn.execute("PRAGMA table_info(pair_verdicts)")}
        if "match_polarity" not in verdict_cols:
            conn.execute(
                "ALTER TABLE pair_verdicts "
                "ADD COLUMN match_polarity TEXT NOT NULL DEFAULT 'unknown'"
            )
        approved_cols = {r["name"] for r in conn.execute("PRAGMA table_info(approved_pairs)")}
        if "match_polarity" not in approved_cols:
            conn.execute(
                "ALTER TABLE approved_pairs "
                "ADD COLUMN match_polarity TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "verdict_id" not in approved_cols:
            conn.execute("ALTER TABLE approved_pairs ADD COLUMN verdict_id INTEGER")
        if "decision_context" not in approved_cols:
            conn.execute("ALTER TABLE approved_pairs ADD COLUMN decision_context TEXT")
        rejected_cols = {r["name"] for r in conn.execute("PRAGMA table_info(rejected_pairs)")}
        if "verdict_id" not in rejected_cols:
            conn.execute("ALTER TABLE rejected_pairs ADD COLUMN verdict_id INTEGER")
        if "decision_context" not in rejected_cols:
            conn.execute("ALTER TABLE rejected_pairs ADD COLUMN decision_context TEXT")
        # paper_fills settlement columns (idempotent)
        fill_cols = {r["name"] for r in conn.execute("PRAGMA table_info(paper_fills)")}
        if "realized_outcome" not in fill_cols:
            conn.execute("ALTER TABLE paper_fills ADD COLUMN realized_outcome TEXT")
        if "realized_pnl_usd" not in fill_cols:
            conn.execute("ALTER TABLE paper_fills ADD COLUMN realized_pnl_usd REAL")
        if "settled_ts" not in fill_cols:
            conn.execute("ALTER TABLE paper_fills ADD COLUMN settled_ts INTEGER")
        if "settle_method" not in fill_cols:
            conn.execute("ALTER TABLE paper_fills ADD COLUMN settle_method TEXT")
        # Indices on the now-guaranteed columns (after the ALTERs above).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_fills_realized "
            "ON paper_fills(realized_outcome)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_fills_pair "
            "ON paper_fills(pair_id)"
        )
        conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
