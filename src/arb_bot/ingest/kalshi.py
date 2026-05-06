import base64
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from arb_bot.config import Config
from arb_bot.db import transaction

log = logging.getLogger(__name__)


@dataclass
class KalshiAuth:
    access_key: str
    private_key: object

    @classmethod
    def from_config(cls, cfg: Config) -> "KalshiAuth":
        pem_path: Path = cfg.kalshi_private_key_path
        with pem_path.open("rb") as f:
            pk = serialization.load_pem_private_key(f.read(), password=None)
        return cls(access_key=cfg.kalshi_access_key, private_key=pk)

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts_ms = str(int(time.time() * 1000))
        message = f"{ts_ms}{method.upper()}{path}".encode()
        sig = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.access_key,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "Content-Type": "application/json",
        }


class KalshiClient:
    """Kalshi REST client.

    IMPORTANT: Kalshi's RSA-PSS signature is computed over the FULL host-relative
    path (e.g., "/trade-api/v2/portfolio/balance"), not just the segment after
    the API version prefix. We split the configured KALSHI_BASE_URL into
    scheme+host vs path-prefix so callers can pass simple endpoints like
    "/portfolio/balance" and we still sign the right string regardless of
    whether the .env's base URL is "https://host" or "https://host/trade-api/v2".
    """

    def __init__(self, cfg: Config, auth: KalshiAuth | None = None):
        self.cfg = cfg
        self.auth = auth or KalshiAuth.from_config(cfg)
        self.session = requests.Session()
        parts = urlsplit(cfg.kalshi_base_url.rstrip("/"))
        self._scheme_host = f"{parts.scheme}://{parts.netloc}"
        self._path_prefix = parts.path  # "" or "/trade-api/v2"

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        sign_path = f"{self._path_prefix}{endpoint}"
        url = f"{self._scheme_host}{sign_path}"
        headers = self.auth.headers("GET", sign_path)
        r = self.session.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def list_open_markets(
        self, limit: int = 1000, min_volume: int = 100
    ) -> Iterable[dict]:
        """Iterate active Kalshi markets via cursor pagination.

        Kalshi's `/markets` returns ~750K markets total when unfiltered (most
        are stale or never-traded). Pass `min_volume` (in float-precision
        contracts) to filter at the API layer — `100` matches Tracker_Kalshi's
        Phase-1 default and cuts to ~10-20K liquid markets.

        `mve_filter=exclude` drops multivariate-event combo markets, which
        are ~96% noise per Tracker_Kalshi's empirical Phase-1 audit
        (docs/findings/2026-04-11_phase1_bulk_pull.md). Without this
        filter, MVE markets dominate raw market count but have ~zero real
        volume and pollute the embedding/candidate-pair pool.

        Note: Kalshi's API uses status='active' rather than 'open'; we
        accept 'active' from the venue but normalize to 'open' downstream.
        """
        cursor = None
        while True:
            params: dict[str, object] = {
                "status": "open",  # Kalshi API filter; venue still echoes status='active' on rows
                "limit": limit,
                "min_volume": min_volume,
                "mve_filter": "exclude",  # drop multivariate-event combo markets (96% noise)
            }
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params=params)
            for m in data.get("markets", []):
                yield m
            cursor = data.get("cursor")
            if not cursor:
                break


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _extract_market_row(m: dict, now_ts: int) -> tuple:
    """Adapt a Kalshi /markets response row to the unified schema.

    Kalshi field names (April 2026 confirmed):
      yes_bid_dollars / yes_ask_dollars   - already-formatted dollar prices (0.0-1.0)
      no_bid_dollars  / no_ask_dollars
      volume_fp                           - float-precision lifetime volume in contracts
      liquidity_dollars                   - current orderbook depth in USD
      status                              - 'active' | 'finalized' | 'settled' | ...
    Older `yes_bid` (cents) fields are gone. We stick to *_dollars and *_fp.
    """
    yes_bid = _safe_float(m.get("yes_bid_dollars"))
    yes_ask = _safe_float(m.get("yes_ask_dollars"))
    no_bid = _safe_float(m.get("no_bid_dollars"))
    no_ask = _safe_float(m.get("no_ask_dollars"))
    # Normalize Kalshi's 'active' to our unified 'open' status
    status = m.get("status") or "active"
    if status == "active":
        status = "open"
    return (
        "kalshi",
        m["ticker"],
        m.get("title") or m.get("yes_sub_title"),
        m.get("rules_primary"),
        m.get("rules_secondary"),
        None,
        _iso_to_ts(m.get("close_time")),
        _iso_to_ts(m.get("expected_expiration_time") or m.get("expiration_time")),
        yes_bid,
        yes_ask,
        no_bid,
        no_ask,
        _safe_float(m.get("volume_fp")) or 0.0,
        _safe_float(m.get("liquidity_dollars")) or 0.0,
        now_ts,
        now_ts,
        status,
        None,  # raw_json — filled in by upsert_markets if cfg.store_raw_json
    )


def _iso_to_ts(v: str | None) -> int | None:
    if not v:
        return None
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


_UPSERT_SQL = """
    INSERT INTO markets (venue, venue_market_id, title, description,
        resolution_criteria, resolution_source, close_time, resolution_time,
        yes_bid, yes_ask, no_bid, no_ask, volume, liquidity,
        first_seen_ts, last_seen_ts, status, raw_json)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(venue, venue_market_id) DO UPDATE SET
        title=excluded.title,
        description=excluded.description,
        resolution_criteria=excluded.resolution_criteria,
        close_time=excluded.close_time,
        resolution_time=excluded.resolution_time,
        yes_bid=excluded.yes_bid,
        yes_ask=excluded.yes_ask,
        no_bid=excluded.no_bid,
        no_ask=excluded.no_ask,
        volume=excluded.volume,
        liquidity=excluded.liquidity,
        last_seen_ts=excluded.last_seen_ts,
        status=excluded.status,
        raw_json=excluded.raw_json
"""

# Commit every COMMIT_BATCH markets so the WAL doesn't grow unbounded
# during a single ingest cycle. With ~10k markets and ~2KB raw_json each,
# a single transaction would otherwise hold ~20-30MB in WAL until COMMIT;
# more importantly, if anything goes wrong mid-cycle we lose nothing.
COMMIT_BATCH = 500


def upsert_markets(conn: sqlite3.Connection, cfg: Config) -> int:
    client = KalshiClient(cfg)
    now_ts = int(time.time())

    # Audit log entry committed up-front
    conn.execute(
        "INSERT INTO ingestion_runs(venue, started_ts) VALUES (?, ?)", ("kalshi", now_ts)
    )
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()

    count = 0
    skipped = 0
    pending = 0
    min_vol = cfg.kalshi_min_volume
    try:
        for m in client.list_open_markets():
            v = m.get("volume_fp") or 0
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = 0.0
            if v < min_vol:
                skipped += 1
                continue
            row = _extract_market_row(m, now_ts)
            if cfg.store_raw_json:
                row = row[:-1] + (json.dumps(m),)
            conn.execute(_UPSERT_SQL, row)
            count += 1
            pending += 1
            if pending >= COMMIT_BATCH:
                conn.commit()
                log.info(
                    "Kalshi: committed batch — %d markets kept, %d skipped < $%.0f vol",
                    count, skipped, min_vol,
                )
                pending = 0
        conn.commit()
        conn.execute(
            "UPDATE ingestion_runs SET finished_ts=?, markets_upserted=? WHERE id=?",
            (int(time.time()), count, run_id),
        )
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.execute(
            "UPDATE ingestion_runs SET finished_ts=?, error=? WHERE id=?",
            (int(time.time()), str(e), run_id),
        )
        conn.commit()
        raise
    log.info(
        "Kalshi: upserted %d markets (skipped %d below $%.0f volume threshold)",
        count, skipped, min_vol,
    )
    return count
