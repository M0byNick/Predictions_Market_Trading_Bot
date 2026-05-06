"""Refresh Polymarket Global prices for active approved pairs using CLOB.

Discovery (May 5 2026): the existing ingest reads bestBid/bestAsk and
outcomePrices from gamma-api, but those fields are a metadata cache
that can lag the live CLOB order book by 30+ points (verified against
West Ham EPL relegation: gamma=0.345, clob=0.745 — 40-point miss).

Result: every paper signal generated against gamma-only data is bogus.
This script overrides the stored Polymarket yes_bid/yes_ask using the
authoritative CLOB book endpoint for every active approved pair.

Cost per cycle: 2 HTTP calls per pair (1 gamma to resolve YES token id,
1 clob to fetch top-of-book). For ~500 active pairs at 1 req/s rate
limit: ~17 min. Acceptable for hourly cron alongside the gamma ingest.

Usage:
    python scripts/refresh_poly_clob.py            # all active pairs
    python scripts/refresh_poly_clob.py --limit 50 # cap for testing
    python scripts/refresh_poly_clob.py --pair-id <id>  # single pair

The script is idempotent and safe to run alongside the existing
gamma ingest — it only writes the four price columns + last_seen_ts.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_ARB_ROOT = _HERE.parent
_SRC = _ARB_ROOT / "src"
if _SRC.exists():
    sys.path.insert(0, str(_SRC))


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (arb-bot/refresh)"})
RATE_LIMIT_SEC = 0.6  # ~1.6 req/s; both endpoints accept this


def _get(url: str, params: dict | None = None) -> dict | list:
    r = SESSION.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _resolve_yes_token(condition_id: str) -> tuple[str | None, str | None]:
    """Return (yes_clob_token_id, market_question) or (None, None)."""
    data = _get(f"{GAMMA_BASE}/markets",
                params={"condition_ids": condition_id})
    if not isinstance(data, list) or not data:
        return None, None
    m = data[0]
    raw = m.get("clobTokenIds") or "[]"
    try:
        tokens = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None, m.get("question")
    if not tokens:
        return None, m.get("question")
    return str(tokens[0]), m.get("question")


def _fetch_clob_top_of_book(token_id: str) -> tuple[float | None, float | None, float | None]:
    """Return (yes_bid, yes_ask, mid) from /book; falls back to /midpoint."""
    try:
        b = _get(f"{CLOB_BASE}/book", params={"token_id": token_id})
        bids = b.get("bids") or []
        asks = b.get("asks") or []
        # bids sorted ascending in API; best bid = max price
        best_bid = max((float(x["price"]) for x in bids), default=None)
        # asks: best ask = min price
        best_ask = min((float(x["price"]) for x in asks), default=None)
        if best_bid is not None and best_ask is not None:
            return best_bid, best_ask, (best_bid + best_ask) / 2.0
    except Exception:
        pass
    # fallback: midpoint endpoint
    try:
        d = _get(f"{CLOB_BASE}/midpoint", params={"token_id": token_id})
        mid = float(d.get("mid"))
        # synth tight spread; not ideal but better than gamma stale
        return mid - 0.005, mid + 0.005, mid
    except Exception:
        return None, None, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--pair-id", type=str, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("refresh_poly_clob")

    load_dotenv(_ARB_ROOT / ".env", override=True)
    from arb_bot.config import load_config
    from arb_bot.db import connect, init_schema

    cfg = load_config()
    init_schema(cfg.db_path)

    with connect(cfg.db_path) as conn:
        if args.pair_id:
            rows = conn.execute(
                "SELECT pair_id, poly_global_market_id FROM approved_pairs "
                "WHERE pair_id=? AND active=1", (args.pair_id,)
            ).fetchall()
        else:
            sql = ("SELECT pair_id, poly_global_market_id FROM approved_pairs "
                   "WHERE active=1")
            if args.limit:
                sql += f" LIMIT {int(args.limit)}"
            rows = conn.execute(sql).fetchall()

        log.info("refreshing CLOB prices for %d active approved pairs", len(rows))
        seen_cond: dict[str, tuple[float | None, float | None, float | None, str | None]] = {}
        n_ok = 0
        n_no_token = 0
        n_no_book = 0
        n_changed_big = 0  # |new - old| > 0.10 -> probably was very stale
        BIG_DELTA = 0.10

        for i, r in enumerate(rows, 1):
            cond_id = r["poly_global_market_id"]
            if not cond_id:
                continue
            # Memoize per condition_id (multiple pairs can share, e.g. same poly market)
            if cond_id in seen_cond:
                yb, ya, mid, q = seen_cond[cond_id]
            else:
                time.sleep(RATE_LIMIT_SEC)
                token_id, question = _resolve_yes_token(cond_id)
                if not token_id:
                    n_no_token += 1
                    log.warning("no YES token for %s (%s)", cond_id[:20], r["pair_id"][:60])
                    seen_cond[cond_id] = (None, None, None, question)
                    continue
                time.sleep(RATE_LIMIT_SEC)
                yb, ya, mid = _fetch_clob_top_of_book(token_id)
                q = question
                seen_cond[cond_id] = (yb, ya, mid, question)
                if mid is None:
                    n_no_book += 1
                    log.warning("no CLOB book for %s (%s)", cond_id[:20], (question or '')[:60])
                    continue

            if mid is None:
                continue

            # Old stored
            old = conn.execute(
                "SELECT yes_bid, yes_ask FROM markets "
                "WHERE venue='poly_global' AND venue_market_id=?",
                (cond_id,)
            ).fetchone()
            old_mid = None
            if old and old["yes_bid"] is not None and old["yes_ask"] is not None:
                old_mid = (old["yes_bid"] + old["yes_ask"]) / 2.0

            now_ts = int(time.time())
            no_bid = (1.0 - ya) if ya is not None else None
            no_ask = (1.0 - yb) if yb is not None else None
            conn.execute(
                """
                UPDATE markets
                   SET yes_bid=?, yes_ask=?, no_bid=?, no_ask=?, last_seen_ts=?
                 WHERE venue='poly_global' AND venue_market_id=?
                """,
                (yb, ya, no_bid, no_ask, now_ts, cond_id),
            )
            conn.commit()
            n_ok += 1
            if old_mid is not None and abs(mid - old_mid) > BIG_DELTA:
                n_changed_big += 1
                log.info(
                    "BIG MOVE %s: gamma_mid=%.3f -> clob_mid=%.3f (Δ=%+.3f) %s",
                    cond_id[:20], old_mid, mid, mid - old_mid,
                    (q or r["pair_id"])[:60],
                )
            elif args.verbose:
                log.debug(
                    "%s: %.3f (was %.3f)",
                    cond_id[:20], mid,
                    old_mid if old_mid is not None else float('nan'),
                )

            if i % 25 == 0:
                log.info("progress: %d/%d", i, len(rows))

        log.info(
            "done. updated=%d  big_moves=%d  no_token=%d  no_book=%d",
            n_ok, n_changed_big, n_no_token, n_no_book,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
