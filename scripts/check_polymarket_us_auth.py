"""Smoke test for Polymarket US Ed25519 authentication.

Hits an auth-required portfolio endpoint. Same defensive shape as the
Kalshi smoke test: refuses to echo anything that looks like key material,
masks long values, and gives structured diagnostics on each failure mode.

Usage (from Arb_Bot/ directory):
    .venv/bin/python scripts/check_polymarket_us_auth.py
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
_ARB_ROOT = _HERE.parent
if _SRC.exists():
    sys.path.insert(0, str(_SRC))


def _mask(s: str, keep: int = 4) -> str:
    if not s:
        return "<empty>"
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}...{s[-keep:]}"


def _looks_like_key_material(s: str) -> bool:
    if not s:
        return False
    if "BEGIN" in s or "END" in s or "PRIVATE" in s.upper():
        return True
    # Ed25519 base64 secrets are typically 44 chars (32 bytes + padding)
    # or 88 chars (64-byte concatenation). Long pure-base64 strings are
    # treated as key material.
    if len(s) > 40 and re.fullmatch(r"[A-Za-z0-9+/=\s\-_.]+", s) and "/" not in s.lstrip("./"):
        return True
    return False


def _safe_display(s: str) -> str:
    if _looks_like_key_material(s):
        return f"<REDACTED — looks like key material; length {len(s)}>"
    return s


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(_ARB_ROOT / ".env")

    from arb_bot.config import load_config
    from arb_bot.ingest.polymarket_us import PolyUSAuth, PolyUSClient

    # Force a host-local data dir so config doesn't try to mkdir /app/data
    if os.environ.get("DATA_DIR", "").startswith("/app"):
        os.environ["DATA_DIR"] = str(_ARB_ROOT / "data")
        os.environ["HEARTBEAT_PATH"] = str(_ARB_ROOT / "data" / ".heartbeat")

    cfg = load_config()

    print("== Polymarket US auth smoke test ==")
    print(f"  base_url        : {cfg.poly_us_base_url}")
    print(f"  access_key      : {_mask(cfg.poly_us_access_key)}  (length {len(cfg.poly_us_access_key)})")
    print(f"  secret_key      : <not displayed>  (length {len(cfg.poly_us_secret_key)})")
    print()

    # Stage 0: required vars present
    if not cfg.poly_us_access_key:
        print("FAIL: POLY_US_ACCESS_KEY is empty in .env")
        return 1
    if not cfg.poly_us_secret_key:
        print("FAIL: POLY_US_SECRET_KEY is empty in .env")
        return 1

    # Stage 1: secret decodes to a valid Ed25519 key
    try:
        auth = PolyUSAuth.from_config(cfg)
    except Exception as e:
        print(f"FAIL: secret decode/load failed: {type(e).__name__}: {e}")
        print("      Hint: POLY_US_SECRET_KEY must be a base64-encoded Ed25519 secret,")
        print("      shown once at API-key creation time in polymarket.us developer portal.")
        return 1
    print("OK   : POLY_US_SECRET_KEY decoded as Ed25519 private key")

    # Stage 2: signed request to a portfolio endpoint
    client = PolyUSClient(cfg, auth)
    # Try the canonical 'am I authenticated' endpoint per the SDK shape:
    # GET /portfolio/positions or /portfolio/account. We use positions as it's
    # most likely to exist with both empty and populated accounts.
    candidates = ["/portfolio/positions", "/portfolio/account", "/account/profile"]
    last_err = None
    for path in candidates:
        try:
            body = client._get(path)
            print(f"OK   : {path} returned successfully")
            if isinstance(body, dict):
                keys = sorted(body.keys())
                print(f"  response keys: {keys}")
            elif isinstance(body, list):
                print(f"  response: list of {len(body)} items")
            print()
            print("RESULT: Polymarket US Ed25519 auth works end-to-end. Safe to proceed with ingest.")
            return 0
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            text = e.response.text[:300] if e.response is not None else ""
            last_err = (path, status, text)
            if status == 404:
                # path didn't exist; try next candidate
                continue
            # auth or server error — break
            break
        except requests.exceptions.ConnectionError as e:
            print(f"FAIL: network error hitting {path}: {e}")
            return 1

    if last_err:
        path, status, text = last_err
        print(f"FAIL: HTTP {status} on {path}")
        print(f"      response (first 300): {text}")
        if status == 401:
            print("      Hint: 401 = signature rejected. Common causes:")
            print("            (a) access_key UUID doesn't match the secret's keypair")
            print("            (b) clock drift > a few seconds (NTP sync your machine)")
            print("            (c) the secret was truncated when copy/pasted (Ed25519 secrets are 32 or 64 bytes raw)")
        return 1
    print("FAIL: all candidate endpoints returned 404. Has the API surface changed?")
    return 1


if __name__ == "__main__":
    sys.exit(main())
