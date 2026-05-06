"""CLI: validate one or more pairs against fresh live quotes.

    python scripts/validate_pair.py KXMLB-26-NYY__0x...
    python scripts/validate_pair.py --kalshi-ticker KXMLB-26-NYY
    python scripts/validate_pair.py --all-would-trade   # validate every
                                                        # current would_trade signal
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_ARB_ROOT = _HERE.parent
_SRC = _ARB_ROOT / "src"
if _SRC.exists():
    sys.path.insert(0, str(_SRC))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pair_id", nargs="?", default=None)
    parser.add_argument("--kalshi-ticker", type=str, default=None,
                        help="resolve pair_id by kalshi_ticker (active pair)")
    parser.add_argument("--all-would-trade", action="store_true",
                        help="validate every pair with would_trade=1 in latest cycle")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")
    log = logging.getLogger("validate_pair")

    load_dotenv(_ARB_ROOT / ".env", override=True)
    from arb_bot.config import load_config
    from arb_bot.db import connect, init_schema
    from arb_bot.signal.validate import validate_pair_now

    cfg = load_config()
    init_schema(cfg.db_path)

    with connect(cfg.db_path) as conn:
        targets: list[str] = []
        if args.pair_id:
            targets = [args.pair_id]
        elif args.kalshi_ticker:
            r = conn.execute(
                "SELECT pair_id FROM approved_pairs WHERE kalshi_ticker=? AND active=1",
                (args.kalshi_ticker,),
            ).fetchone()
            if r:
                targets = [r["pair_id"]]
        elif args.all_would_trade:
            targets = [
                r[0] for r in conn.execute(
                    "SELECT DISTINCT pair_id FROM paper_signals "
                    "WHERE would_trade=1 AND detected_ts = (SELECT MAX(detected_ts) FROM paper_signals)"
                )
            ]
        else:
            parser.print_help()
            return 1

        if not targets:
            log.warning("no pair(s) to validate")
            return 1

        for pair_id in targets:
            t0 = time.monotonic()
            v = validate_pair_now(cfg, conn, pair_id)
            dt = (time.monotonic() - t0) * 1000
            print()
            print(f"=== {pair_id} ===  ({dt:.0f}ms)")
            print(f"  polarity     : {v.polarity}")
            print(f"  cached  K_mid: {v.cached_kal_mid}    P_mid: {v.cached_poly_mid}")
            print(f"  LIVE    K bid/ask: {v.kal_bid} / {v.kal_ask}    P bid/ask: {v.poly_bid} / {v.poly_ask}")
            print(f"  spread (exec): {v.executable_spread:.4f}")
            print(f"  edge (exec)  : {v.executable_edge_bps:+.0f} bps  (after fees + slippage)")
            print(f"  direction    : {v.direction}")
            print(f"  poly book    : ${v.poly_book_size_usd:.2f}" if v.poly_book_size_usd is not None else "  poly book    : ?")
            print(f"  arb_now      : {'✓ YES' if v.is_arb_now else '✗ NO'}")
            print(f"  reason       : {v.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
