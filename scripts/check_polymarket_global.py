"""Smoke test for Polymarket Global ingest (public, no auth).

Hits the gamma-api markets endpoint and reports counts + a few sample
markets. No credentials needed; this verifies the network path and
response shape parse correctly.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
_ARB_ROOT = _HERE.parent
if _SRC.exists():
    sys.path.insert(0, str(_SRC))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(_ARB_ROOT / ".env")

    if os.environ.get("DATA_DIR", "").startswith("/app"):
        os.environ["DATA_DIR"] = str(_ARB_ROOT / "data")
        os.environ["HEARTBEAT_PATH"] = str(_ARB_ROOT / "data" / ".heartbeat")

    from arb_bot.config import load_config
    from arb_bot.ingest.polymarket_global import PolyGlobalClient, _extract_market_row

    cfg = load_config()
    print("== Polymarket Global smoke test ==")
    print(f"  gamma_url   : {cfg.poly_global_gamma_url}")
    print(f"  clob_url    : {cfg.poly_global_clob_url}")
    print(f"  rate/sec    : {cfg.poly_global_rate_per_sec}")
    print()

    client = PolyGlobalClient(cfg)
    try:
        sample = []
        for i, m in enumerate(client.list_open_markets(limit=20)):
            sample.append(m)
            if i >= 4:
                break
    except requests.exceptions.HTTPError as e:
        print(f"FAIL: HTTP {e.response.status_code if e.response else '?'}")
        if e.response is not None:
            print(f"      body (first 300): {e.response.text[:300]}")
        return 1
    except requests.exceptions.ConnectionError as e:
        print(f"FAIL: network error: {e}")
        return 1

    if not sample:
        print("WARN: gamma-api returned 0 open markets (unusual; check filter params)")
        return 1

    print(f"OK   : pulled {len(sample)} sample open markets")
    print()
    for m in sample[:3]:
        row = _extract_market_row(m, 0)
        title = (row[2] or "")[:60]
        yes_bid, yes_ask = row[8], row[9]
        volume = row[12]
        print(f"  - {title!r:64} yes={yes_bid}/{yes_ask}  vol=${volume:,.0f}")
    print()
    print("RESULT: Polymarket Global public ingest reachable. No auth needed for paper v1.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
