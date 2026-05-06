"""Long-running WebSocket listener for Polymarket CLOB market updates.

Phase 2 replacement for the polling-based refresh_poly_clob.py cron.

Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market and
subscribes to the YES clob_token_ids for every active approved pair.
On each `book` or `price_change` event, updates the markets table
with the new top-of-book best bid + best ask.

vs polling:
  - sub-second freshness (push-based) instead of 30-min poll cycle
  - one persistent connection instead of 442 HTTP calls per cycle
  - no rate-limit math; we're under the WS connection cap by 4 orders
    of magnitude

Run as a daemon. Auto-reconnects on disconnect with exponential backoff.
Heartbeat handled by the websockets library's built-in ping_interval.

Usage:
    python scripts/ws_market_listener.py             # foreground, default config
    python scripts/ws_market_listener.py --once      # exit after first
                                                       reconnect cycle (testing)
    python scripts/ws_market_listener.py --duration 60   # run for 60 sec then exit

Logs to stdout; cron (or launchd) pipes to data/logs/ws_listener.out.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import websockets
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_ARB_ROOT = _HERE.parent
_SRC = _ARB_ROOT / "src"
if _SRC.exists():
    sys.path.insert(0, str(_SRC))


WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ws library will send a client ping every PING_INTERVAL_SEC; if no
# pong within PING_TIMEOUT_SEC the connection is considered dead and
# the recv() loop raises ConnectionClosed -> we reconnect.
PING_INTERVAL_SEC = 15
PING_TIMEOUT_SEC = 30

# Reconnect backoff (capped). After N consecutive failures we wait
# min(BACKOFF_BASE * 2^N, BACKOFF_CAP) seconds before retrying.
BACKOFF_BASE = 1.0
BACKOFF_CAP = 60.0

# Refresh the subscription set from the db every N minutes so newly-
# approved pairs get picked up without a process restart.
RESUB_INTERVAL_MIN = 30


def _setup_logging(verbose: bool) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # websockets library is chatty at DEBUG; clamp it
    logging.getLogger("websockets").setLevel(logging.WARNING)
    return logging.getLogger("ws_listener")


def _load_token_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Return {yes_token_id: condition_id} for active approved pairs.

    Uses gamma /markets in batches to resolve token ids. ~10 batched
    HTTP calls for 500 pairs; takes ~5-10 seconds at startup.
    """
    log = logging.getLogger("ws_listener")

    # Reuse the polling refresher's batch logic
    from refresh_poly_clob import _resolve_yes_tokens_batch, _chunked  # type: ignore

    cond_ids = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT poly_global_market_id FROM approved_pairs "
            "WHERE active=1 AND poly_global_market_id IS NOT NULL"
        )
    ]
    log.info("resolving YES tokens for %d unique condition_ids", len(cond_ids))

    token_to_cond: dict[str, str] = {}
    GAMMA_BATCH = 50
    for chunk in _chunked(cond_ids, GAMMA_BATCH):
        try:
            resolved = _resolve_yes_tokens_batch(chunk)
        except Exception as e:
            log.warning("gamma batch failed (%d ids): %s", len(chunk), e)
            continue
        for cid, (token, _question) in resolved.items():
            if token:
                token_to_cond[token] = cid
        time.sleep(0.1)  # be nice
    log.info("resolved %d token_ids -> condition_ids", len(token_to_cond))
    return token_to_cond


def _apply_book_update(
    conn: sqlite3.Connection,
    cond_id: str,
    bids: list[dict],
    asks: list[dict],
    log: logging.Logger,
) -> bool:
    """Compute top-of-book from bids/asks arrays, update markets row.

    Returns True if a write happened (with a meaningful price).
    """
    try:
        best_bid = max((float(x["price"]) for x in bids), default=None)
        best_ask = min((float(x["price"]) for x in asks), default=None)
    except (KeyError, ValueError, TypeError) as e:
        log.debug("bad book payload for %s: %s", cond_id[:20], e)
        return False
    if best_bid is None or best_ask is None:
        return False
    no_bid = 1.0 - best_ask
    no_ask = 1.0 - best_bid
    now_ts = int(time.time())
    conn.execute(
        """
        UPDATE markets
           SET yes_bid=?, yes_ask=?, no_bid=?, no_ask=?, last_seen_ts=?
         WHERE venue='poly_global' AND venue_market_id=?
        """,
        (best_bid, best_ask, no_bid, no_ask, now_ts, cond_id),
    )
    conn.commit()
    return True


def _apply_price_change(
    conn: sqlite3.Connection,
    cond_id: str,
    best_bid_str: str | None,
    best_ask_str: str | None,
    log: logging.Logger,
) -> bool:
    """Update markets row from a price_change event's best_bid/best_ask."""
    try:
        best_bid = float(best_bid_str) if best_bid_str is not None else None
        best_ask = float(best_ask_str) if best_ask_str is not None else None
    except (ValueError, TypeError):
        return False
    if best_bid is None or best_ask is None:
        return False
    no_bid = 1.0 - best_ask
    no_ask = 1.0 - best_bid
    now_ts = int(time.time())
    conn.execute(
        """
        UPDATE markets
           SET yes_bid=?, yes_ask=?, no_bid=?, no_ask=?, last_seen_ts=?
         WHERE venue='poly_global' AND venue_market_id=?
        """,
        (best_bid, best_ask, no_bid, no_ask, now_ts, cond_id),
    )
    conn.commit()
    return True


async def run_once(
    conn: sqlite3.Connection,
    token_to_cond: dict[str, str],
    log: logging.Logger,
    duration: float | None = None,
) -> tuple[int, int]:
    """One connect-subscribe-listen cycle. Returns (n_book, n_price_change).

    Raises on disconnect; caller wraps with backoff loop.
    """
    if not token_to_cond:
        log.error("no token_ids to subscribe to; aborting")
        return (0, 0)

    sub = {
        "assets_ids": list(token_to_cond.keys()),
        "type": "market",
        # Skip custom_feature_enabled: avoids the new_market firehose.
    }
    log.info("connecting to %s (subscribing to %d tokens)",
             WS_URL, len(token_to_cond))

    n_book = n_price = n_other = n_filtered = 0
    t_start = time.monotonic()
    deadline = t_start + duration if duration else None
    next_heartbeat_at = t_start + 60  # first log after 60s, then every 60s

    async with websockets.connect(
        WS_URL,
        ping_interval=PING_INTERVAL_SEC,
        ping_timeout=PING_TIMEOUT_SEC,
        max_size=8 * 1024 * 1024,  # initial book payloads can be big
    ) as ws:
        await ws.send(json.dumps(sub))
        log.info("subscription sent; entering recv loop")

        while True:
            if deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
            else:
                raw = await ws.recv()

            # Server sends both single objects and arrays of objects
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.debug("non-json frame ignored (%d bytes)", len(raw))
                continue
            events = msg if isinstance(msg, list) else [msg]

            for ev in events:
                if not isinstance(ev, dict):
                    continue
                ev_type = ev.get("event_type")

                if ev_type == "book":
                    asset_id = ev.get("asset_id")
                    cond_id = token_to_cond.get(asset_id)
                    if not cond_id:
                        n_filtered += 1
                        continue
                    if _apply_book_update(
                        conn, cond_id,
                        ev.get("bids") or [],
                        ev.get("asks") or [],
                        log,
                    ):
                        n_book += 1
                        log.debug("book updated %s", cond_id[:20])

                elif ev_type == "price_change":
                    # `price_changes` is an array; each entry has its own asset_id
                    for pc in ev.get("price_changes") or []:
                        asset_id = pc.get("asset_id")
                        cond_id = token_to_cond.get(asset_id)
                        if not cond_id:
                            n_filtered += 1
                            continue
                        if _apply_price_change(
                            conn, cond_id,
                            pc.get("best_bid"),
                            pc.get("best_ask"),
                            log,
                        ):
                            n_price += 1
                            log.debug(
                                "price_change %s bid=%s ask=%s",
                                cond_id[:20],
                                pc.get("best_bid"), pc.get("best_ask"),
                            )

                elif ev_type in ("last_trade_price", "tick_size_change",
                                 "new_market", "market_resolved"):
                    # ignore for now (could log market_resolved later
                    # to auto-deactivate approved_pairs)
                    n_other += 1

                else:
                    n_other += 1
                    log.debug("unhandled event_type=%r", ev_type)

            # Periodic heartbeat log so we know the listener is alive.
            # Use a "next due" timestamp instead of int(elapsed) % 60 to
            # avoid firing the log many times per second during the
            # 1-sec window where the truncated check is true.
            now = time.monotonic()
            if now >= next_heartbeat_at:
                elapsed = now - t_start
                log.info(
                    "alive %.0fs  book=%d  price_change=%d  other=%d  filtered=%d",
                    elapsed, n_book, n_price, n_other, n_filtered,
                )
                next_heartbeat_at = now + 60

    elapsed = time.monotonic() - t_start
    log.info(
        "session ended after %.1fs  book=%d  price_change=%d  other=%d  filtered=%d",
        elapsed, n_book, n_price, n_other, n_filtered,
    )
    return n_book, n_price


async def main_async(args: argparse.Namespace) -> int:
    log = _setup_logging(args.verbose)
    load_dotenv(_ARB_ROOT / ".env", override=True)

    # Make sibling scripts importable for _resolve_yes_tokens_batch reuse
    sys.path.insert(0, str(_HERE))

    from arb_bot.config import load_config
    from arb_bot.db import connect, init_schema

    cfg = load_config()
    init_schema(cfg.db_path)

    failures = 0
    last_resub = 0.0
    token_to_cond: dict[str, str] = {}

    while True:
        # Refresh subscription set on first run + every RESUB_INTERVAL_MIN
        if (time.monotonic() - last_resub) > RESUB_INTERVAL_MIN * 60:
            with connect(cfg.db_path) as conn:
                token_to_cond = _load_token_map(conn)
            last_resub = time.monotonic()
            if not token_to_cond:
                log.error("token map is empty; sleeping 60s before retry")
                await asyncio.sleep(60)
                continue

        try:
            with connect(cfg.db_path) as conn:
                # Use a tight timeout on the connection so the WS loop
                # doesn't hold a writer lock against the cron.
                conn.execute("PRAGMA busy_timeout=5000")
                await run_once(
                    conn, token_to_cond, log,
                    duration=args.duration,
                )
            failures = 0  # successful session resets backoff
            if args.once or args.duration:
                return 0
        except (websockets.ConnectionClosed,
                ConnectionResetError, OSError) as e:
            failures += 1
            backoff = min(BACKOFF_BASE * (2 ** (failures - 1)), BACKOFF_CAP)
            log.warning("disconnect (%s: %s); reconnecting in %.1fs",
                        type(e).__name__, e, backoff)
            await asyncio.sleep(backoff)
        except KeyboardInterrupt:
            log.info("interrupted; exiting")
            return 0
        except Exception as e:
            failures += 1
            backoff = min(BACKOFF_BASE * (2 ** (failures - 1)), BACKOFF_CAP)
            log.exception("unexpected error: %s; reconnecting in %.1fs",
                          e, backoff)
            await asyncio.sleep(backoff)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true",
                        help="exit after the first successful session")
    parser.add_argument("--duration", type=float, default=None,
                        help="run for N seconds then exit (testing)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
