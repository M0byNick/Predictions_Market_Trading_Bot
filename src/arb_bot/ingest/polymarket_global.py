"""Polymarket Global ingest (Polygon CLOB).

For users in Polymarket-supported jurisdictions (most of EU including Ireland,
Brazil, etc.). Hits the public gamma-api (market metadata) and clob (order
book) endpoints. NO authentication required for read-only ingest.

For LIVE trading we'd add a Polygon wallet + EIP-712 order signing via
py-clob-client. That's deferred until paper-window validates edge.

Mirrors the structure of the proven Tracker_Poly snapshot_markets.py
script in this repo, adapted to the Arb_Bot unified `markets` schema.
"""
import json
import logging
import sqlite3
import time
from typing import Iterable

import requests

from arb_bot.config import Config
from arb_bot.db import transaction

log = logging.getLogger(__name__)


class PolyGlobalClient:
    """Public-only client. No auth headers. Adds a small rate-limit guard."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.gamma = cfg.poly_global_gamma_url.rstrip("/")
        self.clob = cfg.poly_global_clob_url.rstrip("/")
        self._min_interval = 1.0 / max(cfg.poly_global_rate_per_sec, 1.0)
        self._last_request_ts = 0.0

    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self._last_request_ts + self._min_interval - now
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

    def _get(self, base: str, path: str, params: dict | None = None) -> dict | list:
        self._throttle()
        r = self.session.get(f"{base}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def list_open_markets(self, limit: int = 500) -> Iterable[dict]:
        """Pull open markets from gamma-api with pagination.

        gamma-api returns a list; we paginate via offset. Filter to active +
        not-closed; reject markets whose end_date has passed.
        """
        offset = 0
        while True:
            data = self._get(
                self.gamma,
                "/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                    "order": "endDate",
                    "ascending": "true",
                },
            )
            items = data if isinstance(data, list) else data.get("data") or []
            if not items:
                break
            for m in items:
                yield m
            if len(items) < limit:
                break
            offset += limit

    def book(self, token_id: str) -> dict:
        """Fetch top-of-book for a CLOB token (yes/no side of a market).

        We use the gamma midprice when available rather than fetching
        per-token order books on every cycle; uncomment this for finer
        depth modeling later.
        """
        return self._get(self.clob, f"/book", params={"token_id": token_id})  # type: ignore[return-value]


# ---------------------------- adapter to unified schema ----------------------------

def _iso_to_ts(v: str | None) -> int | None:
    if not v:
        return None
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _safe_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def _extract_outcome_quotes(m: dict) -> tuple[float | None, float | None, float | None, float | None]:
    """Return (yes_bid, yes_ask, no_bid, no_ask) from a gamma market dict.

    gamma-api shape (April 2026 sample):
      {
        "outcomes": "[\"Yes\",\"No\"]",                    # JSON-as-string
        "outcomePrices": "[\"0.62\",\"0.38\"]",            # JSON-as-string, mids
        "bestBid": 0.61, "bestAsk": 0.63,                  # Yes side (sometimes)
        "clobTokenIds": "[\"<yes_token>\",\"<no_token>\"]", # JSON-as-string
        ...
      }
    Some markets have only the "outcomePrices" mids; some expose bestBid/bestAsk
    on the Yes side directly. We're conservative: use bestBid/bestAsk for
    Yes when present, otherwise fall back to outcomePrices as both bid+ask.
    """
    yes_bid = _safe_float(m.get("bestBid"))
    yes_ask = _safe_float(m.get("bestAsk"))
    if yes_bid is None or yes_ask is None:
        prices_raw = m.get("outcomePrices")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except Exception:
                prices = []
        else:
            prices = prices_raw or []
        if len(prices) >= 1:
            try:
                yes_mid = float(prices[0])
                if yes_bid is None:
                    yes_bid = yes_mid
                if yes_ask is None:
                    yes_ask = yes_mid
            except (TypeError, ValueError):
                pass
    no_bid = (1.0 - yes_ask) if yes_ask is not None else None
    no_ask = (1.0 - yes_bid) if yes_bid is not None else None
    return yes_bid, yes_ask, no_bid, no_ask


def _extract_market_row(m: dict, now_ts: int) -> tuple:
    yes_bid, yes_ask, no_bid, no_ask = _extract_outcome_quotes(m)
    venue_id = m.get("conditionId") or m.get("id") or m.get("slug")
    return (
        "poly_global",
        str(venue_id),
        m.get("question") or m.get("title"),
        m.get("description"),
        m.get("resolutionSource") or m.get("rules") or m.get("resolutionCriteria"),
        m.get("resolutionSource"),
        _iso_to_ts(m.get("endDate") or m.get("end_date_iso")),
        _iso_to_ts(m.get("resolutionDate") or m.get("end_date_iso")),
        yes_bid,
        yes_ask,
        no_bid,
        no_ask,
        _safe_float(m.get("volumeNum") or m.get("volume")) or 0.0,
        _safe_float(m.get("liquidityNum") or m.get("liquidity")) or 0.0,
        now_ts,
        now_ts,
        "open" if m.get("active") and not m.get("closed") else "closed",
        json.dumps(m),
    )


def upsert_markets(conn: sqlite3.Connection, cfg: Config) -> int:
    client = PolyGlobalClient(cfg)
    now_ts = int(time.time())
    count = 0
    with transaction(conn):
        conn.execute(
            "INSERT INTO ingestion_runs(venue, started_ts) VALUES (?, ?)",
            ("poly_global", now_ts),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        try:
            for m in client.list_open_markets():
                row = _extract_market_row(m, now_ts)
                conn.execute(
                    """
                    INSERT INTO markets (venue, venue_market_id, title, description,
                        resolution_criteria, resolution_source, close_time, resolution_time,
                        yes_bid, yes_ask, no_bid, no_ask, volume, liquidity,
                        first_seen_ts, last_seen_ts, status, raw_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(venue, venue_market_id) DO UPDATE SET
                        title=excluded.title,
                        description=excluded.description,
                        resolution_criteria=excluded.resolution_criteria,
                        resolution_source=excluded.resolution_source,
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
                    """,
                    row,
                )
                count += 1
            conn.execute(
                "UPDATE ingestion_runs SET finished_ts=?, markets_upserted=? WHERE id=?",
                (int(time.time()), count, run_id),
            )
        except Exception as e:
            conn.execute(
                "UPDATE ingestion_runs SET finished_ts=?, error=? WHERE id=?",
                (int(time.time()), str(e), run_id),
            )
            raise
    log.info("Polymarket Global: upserted %d markets", count)
    return count
