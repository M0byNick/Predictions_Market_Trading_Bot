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
import os
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

# Health-status JSON file -- written periodically by the listener and
# read by the dashboard's /ws_status route.
STATUS_FILE = _ARB_ROOT / "data" / "ws_listener_status.json"
STATUS_WRITE_INTERVAL_SEC = 10


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

    Returns True if a write happened (with a meaningful price). On
    sqlite lock contention with the dashboard, logs and skips this
    one update — never crashes the WS connection. Polymarket sends
    book snapshots every 10s anyway so a missed write is recovered
    on the next event for that token.
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
    try:
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
    except sqlite3.OperationalError as e:
        log.debug("book write skipped (%s): %s", cond_id[:20], e)
        return False


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
    try:
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
    except sqlite3.OperationalError as e:
        log.debug("price_change write skipped (%s): %s", cond_id[:20], e)
        return False


def _state_snapshot(
    state: dict,
    n_book: int,
    n_price: int,
    n_other: int,
    n_filtered: int,
    last_message_wall_ts: float,
) -> dict:
    """Build the dict written to ws_listener_status.json each tick.

    Combines per-session counters with cross-session state (process pid,
    boot time, cumulative reconnect count).
    """
    return {
        # process-level (across reconnects)
        "pid": os.getpid(),
        "process_started_wall_ts": state.get("process_started_wall_ts"),
        "reconnect_count": state.get("reconnect_count", 0),
        # current session
        "session_started_wall_ts": state.get("session_started_wall_ts"),
        "connection_status": state.get("connection_status", "unknown"),
        "subscription_count": state.get("subscription_count", 0),
        "last_message_wall_ts": last_message_wall_ts,
        "n_book": n_book,
        "n_price_change": n_price,
        "n_filtered": n_filtered,
        "n_other": n_other,
        # cumulative across reconnects
        "cum_book": state.get("cum_book", 0) + n_book,
        "cum_price_change": state.get("cum_price_change", 0) + n_price,
    }


def _write_status(state: dict, log: logging.Logger) -> None:
    """Atomically dump the listener's runtime stats for the dashboard.

    Tmpfile + rename so the dashboard never sees a half-written file.
    """
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".tmp")
        # Add wall-clock timestamps next to monotonic ones so the
        # dashboard can compute "X sec ago" without sharing the listener's
        # monotonic clock.
        snapshot = dict(state)
        snapshot["written_at"] = time.time()
        with tmp.open("w") as f:
            json.dump(snapshot, f, indent=2, default=str)
        tmp.replace(STATUS_FILE)
    except Exception as e:
        log.debug("status write failed: %s", e)


async def run_once(
    conn: sqlite3.Connection,
    token_to_cond: dict[str, str],
    log: logging.Logger,
    duration: float | None = None,
    state: dict | None = None,
) -> tuple[int, int]:
    """One connect-subscribe-listen cycle. Returns (n_book, n_price_change).

    Raises on disconnect; caller wraps with backoff loop.
    """
    if state is None:
        state = {}
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
    next_status_write_at = t_start + STATUS_WRITE_INTERVAL_SEC
    last_message_wall_ts = time.time()
    state["session_started_wall_ts"] = time.time()
    state["subscription_count"] = len(token_to_cond)
    state["connection_status"] = "connecting"

    async with websockets.connect(
        WS_URL,
        ping_interval=PING_INTERVAL_SEC,
        ping_timeout=PING_TIMEOUT_SEC,
        max_size=8 * 1024 * 1024,  # initial book payloads can be big
    ) as ws:
        await ws.send(json.dumps(sub))
        log.info("subscription sent; entering recv loop")
        state["connection_status"] = "connected"
        _write_status(_state_snapshot(state, n_book, n_price, n_other,
                                      n_filtered, last_message_wall_ts), log)

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
            last_message_wall_ts = time.time()
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
            if now >= next_status_write_at:
                _write_status(_state_snapshot(state, n_book, n_price, n_other,
                                              n_filtered, last_message_wall_ts), log)
                next_status_write_at = now + STATUS_WRITE_INTERVAL_SEC

    elapsed = time.monotonic() - t_start
    log.info(
        "session ended after %.1fs  book=%d  price_change=%d  other=%d  filtered=%d",
        elapsed, n_book, n_price, n_other, n_filtered,
    )
    # Roll session counters into cumulative running totals so /ws_status
    # shows lifetime numbers, not just current-session numbers.
    state["cum_book"] = state.get("cum_book", 0) + n_book
    state["cum_price_change"] = state.get("cum_price_change", 0) + n_price
    state["connection_status"] = "disconnected"
    _write_status(_state_snapshot(state, 0, 0, 0, 0, last_message_wall_ts), log)
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
    state: dict = {
        "process_started_wall_ts": time.time(),
        "reconnect_count": 0,
        "cum_book": 0,
        "cum_price_change": 0,
    }

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
                # Generous busy_timeout so the WS write loop waits out
                # dashboard reads instead of crashing the connection.
                # Lock-error-on-write is also caught per-update inside
                # _apply_book_update / _apply_price_change as a second
                # line of defense.
                conn.execute("PRAGMA busy_timeout=15000")
                await run_once(
                    conn, token_to_cond, log,
                    duration=args.duration,
                    state=state,
                )
            failures = 0  # successful session resets backoff
            if args.once or args.duration:
                return 0
            # Clean disconnect (e.g. server-initiated close); count as
            # reconnect so the dashboard sees flap rate.
            state["reconnect_count"] = state.get("reconnect_count", 0) + 1
        except (websockets.ConnectionClosed,
                ConnectionResetError, OSError) as e:
            failures += 1
            state["reconnect_count"] = state.get("reconnect_count", 0) + 1
            state["connection_status"] = "reconnecting"
            backoff = min(BACKOFF_BASE * (2 ** (failures - 1)), BACKOFF_CAP)
            log.warning("disconnect (%s: %s); reconnecting in %.1fs",
                        type(e).__name__, e, backoff)
            await asyncio.sleep(backoff)
        except KeyboardInterrupt:
            log.info("interrupted; exiting")
            state["connection_status"] = "stopped"
            _write_status(_state_snapshot(state, 0, 0, 0, 0, time.time()), log)
            return 0
        except Exception as e:
            failures += 1
            state["reconnect_count"] = state.get("reconnect_count", 0) + 1
            state["connection_status"] = "error"
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
