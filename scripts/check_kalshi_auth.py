"""Smoke test for Kalshi RSA-PSS authentication.

Hits /portfolio/balance (auth-required endpoint). Prints structured
diagnostics on each failure mode so we know whether the issue is a
missing file, a malformed PEM, a wrong access key, a bad base URL,
or a clock-drift / signing problem.

Never prints the private key or the full access key to chat-safe output —
only the first 4 + last 4 chars of the access key UUID.

Usage (from Arb_Bot/ directory):
    python -m venv .venv && source .venv/bin/activate
    pip install -e .
    python scripts/check_kalshi_auth.py
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv

# Add src to path so script works without `pip install -e .`
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
_ARB_ROOT = _HERE.parent
if _SRC.exists():
    sys.path.insert(0, str(_SRC))


def _dev_mode_path_remap() -> dict[str, tuple[str, str]]:
    """Rewrite Docker paths (/app/...) in env to host-local paths so the
    smoke test works on a developer laptop without modifying .env. Returns
    a dict of {var_name: (old_value, new_value)} for the report.
    """
    # Load .env first so we can see its values
    load_dotenv(_ARB_ROOT / ".env")
    remapped: dict[str, tuple[str, str]] = {}

    # DATA_DIR: /app/data -> ./data (host) ; if no DATA_DIR, default to a tmpdir
    data_dir = os.environ.get("DATA_DIR", "")
    if data_dir.startswith("/app"):
        new = str(_ARB_ROOT / "data")
        os.environ["DATA_DIR"] = new
        remapped["DATA_DIR"] = (data_dir, new)

    # HEARTBEAT_PATH: /app/data/.heartbeat -> ./data/.heartbeat
    hb = os.environ.get("HEARTBEAT_PATH", "")
    if hb.startswith("/app"):
        new = str(_ARB_ROOT / "data" / ".heartbeat")
        os.environ["HEARTBEAT_PATH"] = new
        remapped["HEARTBEAT_PATH"] = (hb, new)

    # KALSHI_PRIVATE_KEY_PATH: /app/secrets/kalshi.pem -> ./secrets/kalshi.pem
    pkp = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    if pkp.startswith("/app"):
        new = str(_ARB_ROOT / "secrets" / "kalshi.pem")
        os.environ["KALSHI_PRIVATE_KEY_PATH"] = new
        remapped["KALSHI_PRIVATE_KEY_PATH"] = (pkp, new)

    return remapped


from arb_bot.config import load_config  # noqa: E402
from arb_bot.ingest.kalshi import KalshiAuth, KalshiClient  # noqa: E402


def _mask(s: str, keep: int = 4) -> str:
    if not s:
        return "<empty>"
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}...{s[-keep:]}"


def _looks_like_key_material(s: str) -> bool:
    """Heuristic to refuse to print things that look like a key body
    (long, contains BEGIN/END marker text, or is mostly base64). Defends
    against misconfigurations where the user pasted the key contents into
    a path-shaped env var."""
    if not s:
        return False
    if "BEGIN" in s or "END" in s or "PRIVATE" in s.upper():
        return True
    if len(s) > 200:
        return True
    # Long stretch of base64 chars (alnum + / + + + =)
    import re

    if len(s) > 80 and re.fullmatch(r"[A-Za-z0-9+/=\s\-_.]+", s):
        # Long, only base64-ish chars, no path separators except a leading dot/slash maybe
        if "/" not in s.lstrip("./") and "\\" not in s:
            return True
    return False


def _safe_path_display(p) -> str:
    s = str(p)
    if _looks_like_key_material(s):
        return f"<REDACTED — looks like key material, not a path; length {len(s)}>"
    return s


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    remapped = _dev_mode_path_remap()
    cfg = load_config()

    print("== Kalshi auth smoke test ==")
    if remapped:
        print("  dev-mode path remap (host vs Docker):")
        for k, (old, new) in remapped.items():
            print(f"    {k}: {_safe_path_display(old)}  ->  {_safe_path_display(new)}")
        print()
    print(f"  base_url        : {cfg.kalshi_base_url}")
    print(f"  access_key      : {_mask(cfg.kalshi_access_key)}  (length {len(cfg.kalshi_access_key)})")
    print(f"  private_key_path: {_safe_path_display(cfg.kalshi_private_key_path)}")
    print()

    # Stage 0: misconfiguration guard — refuse to proceed if the path
    # variable looks like key material (user pasted key contents into the
    # path env var instead of a filesystem path).
    if _looks_like_key_material(str(cfg.kalshi_private_key_path)):
        print("FAIL: KALSHI_PRIVATE_KEY_PATH appears to contain KEY MATERIAL, not a filesystem path.")
        print("      The .env variable should hold a path like ./secrets/kalshi.pem")
        print("      The actual key file goes at that path.")
        print("      ACTION: (1) revoke this Kalshi keypair on the dashboard,")
        print("              (2) generate a new keypair and save the .pem file directly,")
        print("              (3) edit .env so KALSHI_PRIVATE_KEY_PATH=./secrets/kalshi.pem")
        return 1

    # Stage 1: file existence
    if not cfg.kalshi_access_key:
        print("FAIL: KALSHI_ACCESS_KEY is empty in .env")
        return 1
    if not cfg.kalshi_private_key_path.exists():
        print(f"FAIL: private key file does not exist at {_safe_path_display(cfg.kalshi_private_key_path)}")
        print("      Hint: paths in .env are resolved relative to your shell CWD.")
        print("      Run from Prediction_Markets/Arb_Bot/, or use an absolute path.")
        return 1
    print("OK   : private key file exists")

    # Stage 2: PEM load
    try:
        auth = KalshiAuth.from_config(cfg)
    except Exception as e:
        print(f"FAIL: PEM load failed: {type(e).__name__}: {e}")
        print("      Hint: the file must be an unencrypted RSA private key in PEM format,")
        print("      starting with '-----BEGIN PRIVATE KEY-----' or '-----BEGIN RSA PRIVATE KEY-----'.")
        return 1
    print("OK   : PEM loaded as RSA private key")

    # Stage 3: signed request
    client = KalshiClient(cfg, auth)
    try:
        body = client._get("/portfolio/balance")
    except requests.exceptions.ConnectionError as e:
        print(f"FAIL: network error: {e}")
        print("      Hint: check the base_url is reachable from this machine.")
        return 1
    except requests.exceptions.Timeout as e:
        print(f"FAIL: request timed out: {e}")
        return 1
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        text = e.response.text[:500] if e.response is not None else ""
        print(f"FAIL: HTTP {status}")
        print(f"      response (first 500 chars): {text}")
        if status == 401:
            print("      Hint: 401 = signature rejected. Common causes:")
            print("            (a) access_key UUID doesn't match the PEM's keypair")
            print("            (b) clock drift > a few seconds (run `sntp -sS time.apple.com` or NTP sync)")
            print("            (c) PEM is for the demo env but base_url is prod (or vice versa)")
        elif status == 403:
            print("      Hint: 403 = authenticated but not authorized for this endpoint.")
        elif status == 404:
            print("      Hint: 404 = endpoint not found. Verify base_url ends with /trade-api/v2")
        return 1
    except Exception as e:
        print(f"FAIL: unexpected error: {type(e).__name__}: {e}")
        return 1

    print("OK   : /portfolio/balance returned successfully")
    print()
    # Don't dump the whole body — could include user-identifying info.
    # Just confirm the shape and a single sanitized field.
    if isinstance(body, dict):
        keys = sorted(body.keys())
        print(f"  response keys: {keys}")
        if "balance" in body:
            print(f"  balance (cents): {body['balance']}")
    print()
    print("RESULT: Kalshi RSA-PSS auth works end-to-end. Safe to proceed with ingest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
