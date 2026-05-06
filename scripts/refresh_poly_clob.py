"""Refresh Polymarket Global prices for active approved pairs using CLOB.

Discovery (May 5 2026): the existing ingest reads bestBid/bestAsk and
outcomePrices from gamma-api, but those fields are a metadata cache
that can lag the live CLOB order book by 30+ points (verified against
West Ham EPL relegation: gamma=0.345, clob=0.745 — 40-point miss).

Result: every paper signal generated against gamma-only data is bogus.
This script overrides the stored Polymarket yes_bid/yes_ask using the
authoritative CLOB book endpoint for every active approved pair.

Phase 1 perf (May 6 2026): batched gamma + 10 req/s rate. Polymarket's
documented limits are: gamma /markets 300/10s, CLOB /book 1500/10s.
At 10 req/s we're 15-90x under the limits. ~534 pairs in ~60 seconds.

Usage:
    python scripts/refresh_poly_clob.py            # all active pairs
    python scripts/refresh_poly_clob.py --limit 50 # cap for testing
    python scripts/refresh_poly_clob.py --pair-id <id>  # single pair
    python scripts/refresh_poly_clob.py --batch-size 50  # gamma chunk

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

# Phase 1 rate limit: 10 req/s = 100 req/10s. Documented limits are
# gamma /markets=300/10s and CLOB /book=1500/10s, so 15-90x headroom.
# If we ever see Cloudflare 429s, bump this back up.
RATE_LIMIT_SEC = 0.1

# Gamma /markets accepts comma-separated condition_ids. URL length limit
# is ~8KB; each conditionId is 66 chars + "," = ~67 chars. 50 IDs = ~3.4KB,
# well within safe URL length.
GAMMA_BATCH_DEFAULT = 50

# Treat |new - old| > BIG_DELTA as evidence the gamma cache was wildly
# stale. Logged so chronic-drift markets are visible across runs.
BIG_DELTA = 0.10


def _get(url: str, params: dict | None = None) -> dict | list:
    r = SESSION.get(url, params=params, timeout=20)
    if r.status_code == 429:
        # Cloudflare throttle — back off and retry once
        retry_after = int(r.headers.get("Retry-After", "2"))
        time.sleep(min(retry_after, 10))
        r = SESSION.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _resolve_yes_tokens_batch(
    condition_ids: list[str],
) -> dict[str, tuple[str | None, str | None]]:
    """Batch-resolve YES clob_token_ids via gamma /markets?condition_ids=A,B,...

    Returns: {condition_id: (yes_token_id_or_None, question_or_None)}.
    Missing entries (gamma didn't return that conditionId) get (None, None).
    """
    if not condition_ids:
        return {}
    out: dict[str, tuple[str | None, str | None]] = {
        cid: (None, None) for cid in condition_ids
    }
    # Gamma expects repeated query params, not comma-separated:
    #   ?condition_ids=A&condition_ids=B&condition_ids=C
    # requests encodes a list value into repeated params automatically.
    # IMPORTANT: gamma's default `limit` is 20 — without overriding, a
    # 50-id batch silently returns only 20 rows. Set limit=batch+slack.
    data = _get(
        f"{GAMMA_BASE}/markets",
        params={
            "condition_ids": condition_ids,
            "limit": max(len(condition_ids) * 2, 100),
        },
    )
    items = data if isinstance(data, list) else (data.get("data") or [])
    for m in items:
        cid = m.get("conditionId")
        if not cid:
            continue
        raw = m.get("clobTokenIds") or "[]"
        try:
            tokens = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            out[cid] = (None, m.get("question"))
            continue
        if not tokens:
            out[cid] = (None, m.get("question"))
        else:
            out[cid] = (str(tokens[0]), m.get("question"))
    return out


def _fetch_clob_top_of_book(
    token_id: str,
) -> tuple[float | None, float | None, float | None]:
    """Return (yes_bid, yes_ask, mid) from /book; falls back to /midpoint."""
    try:
        b = _get(f"{CLOB_BASE}/book", params={"token_id": token_id})
        bids = b.get("bids") or []
        asks = b.get("asks") or []
        best_bid = max((float(x["price"]) for x in bids), default=None)
        best_ask = min((float(x["price"]) for x in asks), default=None)
        if best_bid is not None and best_ask is not None:
            return best_bid, best_ask, (best_bid + best_ask) / 2.0
    except Exception:
        pass
    try:
        d = _get(f"{CLOB_BASE}/midpoint", params={"token_id": token_id})
        mid = float(d.get("mid"))
        return mid - 0.005, mid + 0.005, mid
    except Exception:
        return None, None, None


def _chunked(seq: list[str], n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--pair-id", type=str, default=None)
    parser.add_argument("--batch-size", type=int, default=GAMMA_BATCH_DEFAULT,
                        help=f"gamma batch size (default {GAMMA_BATCH_DEFAULT})")
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

    t0 = time.monotonic()

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

        # Dedupe condition_ids (multiple pairs can share the same poly market)
        cond_to_pairs: dict[str, list[str]] = {}
        for r in rows:
            cid = r["poly_global_market_id"]
            if not cid:
                continue
            cond_to_pairs.setdefault(cid, []).append(r["pair_id"])
        unique_cids = list(cond_to_pairs.keys())

        log.info(
            "refreshing CLOB prices: %d pairs / %d unique condition_ids",
            len(rows), len(unique_cids),
        )

        # Phase A: batch-resolve all YES tokens via gamma
        token_map: dict[str, tuple[str | None, str | None]] = {}
        n_gamma_calls = 0
        for chunk in _chunked(unique_cids, args.batch_size):
            time.sleep(RATE_LIMIT_SEC)
            try:
                resolved = _resolve_yes_tokens_batch(chunk)
            except Exception as e:
                log.warning("gamma batch failed (%d ids): %s — falling back per-id", len(chunk), e)
                resolved = {}
                for cid in chunk:
                    time.sleep(RATE_LIMIT_SEC)
                    try:
                        single = _resolve_yes_tokens_batch([cid])
                        resolved.update(single)
                    except Exception as e2:
                        log.warning("  per-id fallback failed for %s: %s", cid[:20], e2)
                        resolved[cid] = (None, None)
            token_map.update(resolved)
            n_gamma_calls += 1
        log.info("gamma resolved: %d tokens via %d batched calls",
                 sum(1 for tk, _ in token_map.values() if tk), n_gamma_calls)

        # Phase B: per-token CLOB book fetch + db update
        n_ok = 0
        n_no_token = 0
        n_no_book = 0
        n_changed_big = 0
        n_clob_calls = 0

        for i, cid in enumerate(unique_cids, 1):
            yes_token, question = token_map.get(cid, (None, None))
            if not yes_token:
                n_no_token += 1
                if args.verbose:
                    log.debug("no YES token: %s (%s)", cid[:20],
                              (question or cond_to_pairs[cid][0])[:60])
                continue

            time.sleep(RATE_LIMIT_SEC)
            yb, ya, mid = _fetch_clob_top_of_book(yes_token)
            n_clob_calls += 1
            if mid is None:
                n_no_book += 1
                log.warning("no CLOB book for %s (%s)", cid[:20], (question or '')[:60])
                continue

            old = conn.execute(
                "SELECT yes_bid, yes_ask FROM markets "
                "WHERE venue='poly_global' AND venue_market_id=?",
                (cid,)
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
                (yb, ya, no_bid, no_ask, now_ts, cid),
            )
            conn.commit()
            n_ok += 1
            if old_mid is not None and abs(mid - old_mid) > BIG_DELTA:
                n_changed_big += 1
                log.info(
                    "BIG MOVE %s: was=%.3f -> clob=%.3f (Δ=%+.3f) %s",
                    cid[:20], old_mid, mid, mid - old_mid,
                    (question or cond_to_pairs[cid][0])[:60],
                )

            if i % 100 == 0:
                log.info("progress: %d/%d", i, len(unique_cids))

        elapsed = time.monotonic() - t0
        log.info(
            "done in %.1fs. updated=%d  big_moves=%d  no_token=%d  no_book=%d  "
            "(gamma_calls=%d clob_calls=%d)",
            elapsed, n_ok, n_changed_big, n_no_token, n_no_book,
            n_gamma_calls, n_clob_calls,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
