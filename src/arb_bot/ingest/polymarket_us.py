import base64
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

from arb_bot.config import Config
from arb_bot.db import transaction

log = logging.getLogger(__name__)


@dataclass
class PolyUSAuth:
    access_key: str
    private_key: ed25519.Ed25519PrivateKey

    @classmethod
    def from_config(cls, cfg: Config) -> "PolyUSAuth":
        raw = base64.b64decode(cfg.poly_us_secret_key)
        pk = ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])
        return cls(access_key=cfg.poly_us_access_key, private_key=pk)

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts_ms = str(int(time.time() * 1000))
        message = f"{ts_ms}{method.upper()}{path}".encode()
        sig = self.private_key.sign(message)
        return {
            "X-PM-Access-Key": self.access_key,
            "X-PM-Timestamp": ts_ms,
            "X-PM-Signature": base64.b64encode(sig).decode(),
            "Content-Type": "application/json",
        }


class PolyUSClient:
    """Polymarket US REST client.

    Same gotcha as Kalshi: the Ed25519 signature is computed over the FULL
    host-relative path (e.g., "/v1/portfolio/positions"), not just the segment
    after the version prefix.
    """

    def __init__(self, cfg: Config, auth: PolyUSAuth | None = None):
        self.cfg = cfg
        self.auth = auth or PolyUSAuth.from_config(cfg)
        self.session = requests.Session()
        parts = urlsplit(cfg.poly_us_base_url.rstrip("/"))
        self._scheme_host = f"{parts.scheme}://{parts.netloc}"
        self._path_prefix = parts.path  # "" or "/v1"

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        sign_path = f"{self._path_prefix}{endpoint}"
        url = f"{self._scheme_host}{sign_path}"
        headers = self.auth.headers("GET", sign_path)
        r = self.session.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def list_open_markets(self, limit: int = 200) -> Iterable[dict]:
        offset = 0
        while True:
            data = self._get("/markets", params={"status": "open", "limit": limit, "offset": offset})
            items = data.get("markets") or data.get("data") or []
            if not items:
                break
            for m in items:
                yield m
            if len(items) < limit:
                break
            offset += limit


def _extract_market_row(m: dict, now_ts: int) -> tuple:
    outcomes = m.get("outcomes") or []
    yes = _find_outcome(outcomes, "yes") or {}
    no = _find_outcome(outcomes, "no") or {}
    return (
        "poly_us",
        m.get("marketSlug") or m.get("id") or m.get("slug"),
        m.get("question") or m.get("title"),
        m.get("description"),
        m.get("resolutionCriteria") or m.get("rules"),
        m.get("resolutionSource") or m.get("oracle"),
        _iso_to_ts(m.get("endDate") or m.get("closeTime")),
        _iso_to_ts(m.get("resolutionDate") or m.get("resolveTime")),
        _price(yes.get("bestBid")),
        _price(yes.get("bestAsk")),
        _price(no.get("bestBid")),
        _price(no.get("bestAsk")),
        float(m.get("volume", 0) or 0),
        float(m.get("liquidity", 0) or 0),
        now_ts,
        now_ts,
        m.get("status", "open"),
        json.dumps(m),
    )


def _find_outcome(outcomes: list[dict], name: str) -> dict | None:
    for o in outcomes:
        n = (o.get("name") or o.get("outcome") or "").lower()
        if n == name:
            return o
    return None


def _price(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _iso_to_ts(v: str | None) -> int | None:
    if not v:
        return None
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def upsert_markets(conn: sqlite3.Connection, cfg: Config) -> int:
    client = PolyUSClient(cfg)
    now_ts = int(time.time())
    count = 0
    with transaction(conn):
        conn.execute(
            "INSERT INTO ingestion_runs(venue, started_ts) VALUES (?, ?)", ("poly_us", now_ts)
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
    log.info("Polymarket US: upserted %d markets", count)
    return count
