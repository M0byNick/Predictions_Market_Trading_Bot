"""On-demand pair validation: fresh-fetch both venues, compute REAL
executable edge using bid+ask (not mid).

Why this exists: signal-cycle scans operate on the markets table, which
is updated by background pollers/WS. Two failure modes leak through:

  1. Kalshi side is stale (hourly cron lag, up to 60 min old) — bot may
     generate a signal off a price that has since moved.
  2. Both sides have wide bid/ask spreads — the mid-based spread looks
     like real edge but the executable edge (cross at unfavorable price
     on each leg) is much smaller or negative.

`validate_pair_now(cfg, conn, pair_id)` re-evaluates a single pair using
fresh API hits + bid/ask-based edge math, in ~2 HTTP calls (1 Kalshi,
1 Polymarket). Cheap enough to call as a pre-flight check before every
paper-fill or live-order placement.

Used by:
  - scripts/dry_run_signals.py — second-checks every would_trade
    signal before committing to paper_signals/paper_fills
  - scripts/validate_pair.py — ad-hoc CLI ("is this pair still real
    right now?")
  - dashboard /pair/<pair_id>/validate route — manual refresh button
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

import requests

from arb_bot.config import Config
from arb_bot.signal.spread import (
    _kalshi_taker_fee_per_contract,
    _poly_taker_fee_per_contract,
    _round_trip_slippage_bps,
)

log = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    pair_id: str
    polarity: str  # 'same' | 'inverse' | 'unknown'

    # Live quotes (just-fetched)
    kal_bid: float | None
    kal_ask: float | None
    poly_bid: float | None
    poly_ask: float | None

    # Cached quotes (what the bot was about to trade against)
    cached_kal_mid: float | None
    cached_poly_mid: float | None

    # Realistic executable spread + edge (in bps of $1 notional)
    executable_spread: float          # always positive (or 0 if no arb)
    executable_edge_bps: float        # spread - fees - slippage; can be negative
    direction: str                    # which side to buy/sell, or 'no_arb'

    # Diagnosis
    is_arb_now: bool                  # executable_edge_bps > min_edge threshold
    poly_book_size_usd: float | None  # top-of-book size on Polymarket (for slippage realism)
    reason: str                       # human-readable summary

    def __str__(self) -> str:
        return (
            f"validate({self.pair_id[:50]}): "
            f"K=[{self.kal_bid}/{self.kal_ask}] "
            f"P=[{self.poly_bid}/{self.poly_ask}] "
            f"exec_edge={self.executable_edge_bps:+.0f}bps  "
            f"arb_now={self.is_arb_now}  {self.reason}"
        )


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0 (arb-bot/validate)"})


def _fetch_kalshi_quote(cfg: Config, ticker: str) -> tuple[float | None, float | None]:
    """Single Kalshi /markets?tickers={ticker} call -> (yes_bid, yes_ask).

    Uses the *_dollars fields (newer API). Returns (None, None) on miss.
    Total time ~150ms per call, well under any rate limit.
    """
    from arb_bot.ingest.kalshi import KalshiClient
    try:
        r = KalshiClient(cfg)._get("/markets", params={"tickers": ticker})
        markets = r.get("markets") or []
        if not markets:
            return None, None
        m = markets[0]
        bid = m.get("yes_bid_dollars")
        ask = m.get("yes_ask_dollars")
        return (
            float(bid) if bid not in (None, "", "0", "0.00", 0) else float(bid) if bid is not None else None,
            float(ask) if ask is not None else None,
        )
    except Exception as e:
        log.warning("kalshi fetch failed for %s: %s", ticker, e)
        return None, None


def _fetch_poly_quote_via_clob(
    cfg: Config, condition_id: str
) -> tuple[float | None, float | None, float | None]:
    """Resolve YES token via gamma, then fetch CLOB top-of-book.

    Returns (best_bid, best_ask, top_of_book_usd_size). top_of_book_usd_size
    is sum of resting size at best-bid AND best-ask (rough liquidity proxy).
    Total time ~300ms (1 gamma + 1 clob call).
    """
    try:
        # Resolve YES token id
        g = _SESSION.get(
            f"{GAMMA_BASE}/markets",
            params={"condition_ids": [condition_id], "limit": 5},
            timeout=10,
        )
        g.raise_for_status()
        markets = g.json()
        if not markets:
            return None, None, None
        tokens_raw = markets[0].get("clobTokenIds") or "[]"
        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        if not tokens:
            return None, None, None
        yes_token = str(tokens[0])

        # Fetch CLOB top-of-book
        b = _SESSION.get(
            f"{CLOB_BASE}/book",
            params={"token_id": yes_token},
            timeout=10,
        )
        b.raise_for_status()
        book = b.json()
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = max((float(x["price"]) for x in bids), default=None)
        best_ask = min((float(x["price"]) for x in asks), default=None)

        # Top-of-book size (sum of resting at best price each side, in $ notional)
        usd_size = 0.0
        for x in bids:
            if best_bid and abs(float(x["price"]) - best_bid) < 0.0005:
                usd_size += float(x["price"]) * float(x["size"])
        for x in asks:
            if best_ask and abs(float(x["price"]) - best_ask) < 0.0005:
                usd_size += float(x["price"]) * float(x["size"])

        return best_bid, best_ask, usd_size
    except Exception as e:
        log.warning("poly fetch failed for %s: %s", condition_id, e)
        return None, None, None


def _round_trip_fee_bps_at_exec(
    kal_exec_price: float, poly_exec_price: float
) -> float:
    """Fee in bps of $1 notional, evaluated at the actual fill prices."""
    return (
        _kalshi_taker_fee_per_contract(kal_exec_price)
        + _poly_taker_fee_per_contract(poly_exec_price)
    ) * 10_000


def _compute_executable_edge_same(
    cfg: Config,
    kal_bid: float, kal_ask: float,
    poly_bid: float, poly_ask: float,
) -> tuple[float, float, str]:
    """Same-polarity arb: BUY cheap leg's ASK, SELL expensive leg's BID.

    Returns (executable_spread, executable_edge_bps, direction).
    """
    kal_mid = (kal_bid + kal_ask) / 2.0
    poly_mid = (poly_bid + poly_ask) / 2.0
    if kal_mid > poly_mid:
        # SELL Kalshi at bid, BUY Polymarket at ask
        spread = kal_bid - poly_ask
        direction = "sell_kalshi_yes_buy_poly_yes"
        kal_exec, poly_exec = kal_bid, poly_ask
    elif poly_mid > kal_mid:
        # SELL Polymarket at bid, BUY Kalshi at ask
        spread = poly_bid - kal_ask
        direction = "buy_kalshi_yes_sell_poly_yes"
        kal_exec, poly_exec = kal_ask, poly_bid
    else:
        return 0.0, 0.0, "no_arb"

    spread = max(spread, 0.0)  # if it inverted, no-arb
    raw_bps = spread * 10_000
    fee_bps = _round_trip_fee_bps_at_exec(kal_exec, poly_exec)
    slip_bps = _round_trip_slippage_bps(cfg)
    edge_bps = raw_bps - fee_bps - slip_bps
    return spread, edge_bps, direction if edge_bps > 0 else "no_arb"


def _compute_executable_edge_inverse(
    cfg: Config,
    kal_bid: float, kal_ask: float,
    poly_bid: float, poly_ask: float,
) -> tuple[float, float, str]:
    """Inverse-polarity arb: at fair value yes_K + yes_P = 1.

    SELL-both-YES if (kal_bid + poly_bid) > 1: receive cap = sum_bids,
        owe $1 if either fires.
    BUY-both-YES if (kal_ask + poly_ask) < 1: pay sum_asks, receive $1
        if either fires.
    """
    sum_bids = kal_bid + poly_bid
    sum_asks = kal_ask + poly_ask
    if sum_bids > 1.0:
        spread = sum_bids - 1.0
        direction = "sell_both_yes_inverse"
        kal_exec, poly_exec = kal_bid, poly_bid
    elif sum_asks < 1.0:
        spread = 1.0 - sum_asks
        direction = "buy_both_yes_inverse"
        kal_exec, poly_exec = kal_ask, poly_ask
    else:
        return 0.0, 0.0, "no_arb"

    raw_bps = spread * 10_000
    fee_bps = _round_trip_fee_bps_at_exec(kal_exec, poly_exec)
    slip_bps = _round_trip_slippage_bps(cfg)
    edge_bps = raw_bps - fee_bps - slip_bps
    return spread, edge_bps, direction if edge_bps > 0 else "no_arb"


def validate_pair_now(
    cfg: Config, conn: sqlite3.Connection, pair_id: str
) -> ValidationResult:
    """Fetch live quotes + recompute realistic executable edge for ONE pair.

    Cost: 1 Kalshi call + 1 gamma call + 1 CLOB call ~= 0.5-1 second total.
    """
    row = conn.execute(
        """
        SELECT ap.pair_id, ap.kalshi_ticker, ap.poly_global_market_id,
               ap.match_polarity,
               km.yes_bid AS k_cached_bid, km.yes_ask AS k_cached_ask,
               pm.yes_bid AS p_cached_bid, pm.yes_ask AS p_cached_ask
          FROM approved_pairs ap
     LEFT JOIN markets km ON km.venue='kalshi'      AND km.venue_market_id=ap.kalshi_ticker
     LEFT JOIN markets pm ON pm.venue='poly_global' AND pm.venue_market_id=ap.poly_global_market_id
         WHERE ap.pair_id=? AND ap.active=1
        """,
        (pair_id,),
    ).fetchone()
    if not row:
        return ValidationResult(
            pair_id=pair_id, polarity="unknown",
            kal_bid=None, kal_ask=None, poly_bid=None, poly_ask=None,
            cached_kal_mid=None, cached_poly_mid=None,
            executable_spread=0.0, executable_edge_bps=0.0,
            direction="no_arb", is_arb_now=False, poly_book_size_usd=None,
            reason="pair not active or missing in approved_pairs",
        )

    polarity = row["match_polarity"] or "unknown"
    cached_kal_mid = (
        ((row["k_cached_bid"] or 0) + (row["k_cached_ask"] or 0)) / 2.0
        if row["k_cached_bid"] is not None else None
    )
    cached_poly_mid = (
        ((row["p_cached_bid"] or 0) + (row["p_cached_ask"] or 0)) / 2.0
        if row["p_cached_bid"] is not None else None
    )

    # Fresh quotes
    kal_bid, kal_ask = _fetch_kalshi_quote(cfg, row["kalshi_ticker"])
    poly_bid, poly_ask, poly_book_size = _fetch_poly_quote_via_clob(
        cfg, row["poly_global_market_id"]
    )

    if kal_bid is None or kal_ask is None or poly_bid is None or poly_ask is None:
        return ValidationResult(
            pair_id=pair_id, polarity=polarity,
            kal_bid=kal_bid, kal_ask=kal_ask, poly_bid=poly_bid, poly_ask=poly_ask,
            cached_kal_mid=cached_kal_mid, cached_poly_mid=cached_poly_mid,
            executable_spread=0.0, executable_edge_bps=0.0,
            direction="no_arb", is_arb_now=False,
            poly_book_size_usd=poly_book_size,
            reason="quote fetch failed on at least one venue",
        )

    if polarity == "same":
        spread, edge_bps, direction = _compute_executable_edge_same(
            cfg, kal_bid, kal_ask, poly_bid, poly_ask
        )
    elif polarity == "inverse":
        spread, edge_bps, direction = _compute_executable_edge_inverse(
            cfg, kal_bid, kal_ask, poly_bid, poly_ask
        )
    else:
        return ValidationResult(
            pair_id=pair_id, polarity=polarity,
            kal_bid=kal_bid, kal_ask=kal_ask, poly_bid=poly_bid, poly_ask=poly_ask,
            cached_kal_mid=cached_kal_mid, cached_poly_mid=cached_poly_mid,
            executable_spread=0.0, executable_edge_bps=0.0,
            direction="no_arb", is_arb_now=False,
            poly_book_size_usd=poly_book_size,
            reason="polarity unknown",
        )

    is_arb_now = edge_bps >= cfg.paper_min_edge_bps

    if direction == "no_arb":
        reason = f"executable spread <= 0; mids deceiving (K_mid={(kal_bid+kal_ask)/2:.3f} P_mid={(poly_bid+poly_ask)/2:.3f})"
    elif not is_arb_now:
        reason = f"executable_edge {edge_bps:.0f}bps below threshold {cfg.paper_min_edge_bps}bps"
    elif poly_book_size is not None and poly_book_size < 100:
        reason = f"executable arb exists ({edge_bps:.0f}bps) but Polymarket book is THIN (${poly_book_size:.0f})"
    else:
        reason = f"executable arb confirmed: {edge_bps:.0f}bps after fees+slip"

    return ValidationResult(
        pair_id=pair_id, polarity=polarity,
        kal_bid=kal_bid, kal_ask=kal_ask, poly_bid=poly_bid, poly_ask=poly_ask,
        cached_kal_mid=cached_kal_mid, cached_poly_mid=cached_poly_mid,
        executable_spread=spread, executable_edge_bps=edge_bps,
        direction=direction, is_arb_now=is_arb_now,
        poly_book_size_usd=poly_book_size, reason=reason,
    )
