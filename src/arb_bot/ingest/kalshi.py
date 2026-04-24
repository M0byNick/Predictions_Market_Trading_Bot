import base64
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

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
    def __init__(self, cfg: Config, auth: KalshiAuth | None = None):
        self.cfg = cfg
        self.auth = auth or KalshiAuth.from_config(cfg)
        self.session = requests.Session()
        self.base = cfg.kalshi_base_url.rstrip("/")

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        headers = self.auth.headers("GET", path)
        r = self.session.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def list_open_markets(self, limit: int = 1000) -> Iterable[dict]:
        cursor = None
        while True:
            params = {"status": "open", "limit": limit}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params=params)
            for m in data.get("markets", []):
                yield m
            cursor = data.get("cursor")
            if not cursor:
                break


def _extract_market_row(m: dict, now_ts: int) -> tuple:
    yes_bid = _cents_to_price(m.get("yes_bid"))
    yes_ask = _cents_to_price(m.get("yes_ask"))
    no_bid = _cents_to_price(m.get("no_bid"))
    no_ask = _cents_to_price(m.get("no_ask"))
    return (
        "kalshi",
        m["ticker"],
        m.get("title") or m.get("subtitle"),
        m.get("rules_primary"),
        m.get("rules_secondary"),
        None,
        _iso_to_ts(m.get("close_time")),
        _iso_to_ts(m.get("expected_expiration_time") or m.get("expiration_time")),
        yes_bid,
        yes_ask,
        no_bid,
        no_ask,
        float(m.get("volume", 0) or 0),
        float(m.get("liquidity", 0) or 0),
        now_ts,
        now_ts,
        m.get("status", "open"),
        json.dumps(m),
    )


def _cents_to_price(v) -> float | None:
    if v is None:
        return None
    return float(v) / 100.0


def _iso_to_ts(v: str | None) -> int | None:
    if not v:
        return None
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def upsert_markets(conn: sqlite3.Connection, cfg: Config) -> int:
    client = KalshiClient(cfg)
    now_ts = int(time.time())
    count = 0
    with transaction(conn):
        conn.execute(
            "INSERT INTO ingestion_runs(venue, started_ts) VALUES (?, ?)", ("kalshi", now_ts)
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
    log.info("Kalshi: upserted %d markets", count)
    return count
