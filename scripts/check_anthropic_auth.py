"""Smoke test for Anthropic API access.

Single low-cost ping to confirm the key works + the configured model is
reachable. Costs roughly 1¢ per run. Refuses to print the key.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_ARB_ROOT = _HERE.parent
_SRC = _ARB_ROOT / "src"
if _SRC.exists():
    sys.path.insert(0, str(_SRC))


def _looks_valid_key(s: str) -> tuple[bool, str]:
    if not s:
        return False, "empty"
    if s == "sk-ant-..." or s.startswith("sk-ant-...") and len(s) < 30:
        return False, "still the .env.example placeholder"
    if not s.startswith("sk-ant-"):
        return False, "doesn't start with sk-ant-"
    if len(s) < 50:
        return False, f"too short ({len(s)} chars; real keys are ~100+)"
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", s):
        return False, "contains characters that aren't in the API-key alphabet"
    return True, "looks valid"


def main() -> int:
    load_dotenv(_ARB_ROOT / ".env", override=True)

    if os.environ.get("DATA_DIR", "").startswith("/app"):
        os.environ["DATA_DIR"] = str(_ARB_ROOT / "data")

    from arb_bot.config import load_config

    cfg = load_config()
    print("== Anthropic auth smoke test ==")
    print(f"  model        : {cfg.anthropic_model}")
    print(f"  key length   : {len(cfg.anthropic_api_key)} chars")

    ok, why = _looks_valid_key(cfg.anthropic_api_key)
    if not ok:
        print(f"FAIL: ANTHROPIC_API_KEY {why}")
        print("      Edit .env and replace ANTHROPIC_API_KEY=... with your real key")
        print("      from https://console.anthropic.com/settings/keys")
        return 1
    print(f"  key shape    : {why}")
    print()

    from anthropic import Anthropic, APIError, AuthenticationError

    client = Anthropic(api_key=cfg.anthropic_api_key)
    try:
        # Opus 4.x rejects `temperature` (deprecated for that family). We
        # omit it unconditionally; modern Claude models are deterministic
        # enough by default for this structured-JSON use case.
        msg = client.messages.create(
            model=cfg.anthropic_model,
            max_tokens=64,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Reply with the exact JSON {\"ok\": true} and nothing else."
                    ),
                }
            ],
        )
    except AuthenticationError as e:
        print(f"FAIL: 401 authentication: {e}")
        print("      The key is rejected. Either it's wrong, expired, or the workspace")
        print("      is at a billing block. Check console.anthropic.com.")
        return 1
    except APIError as e:
        print(f"FAIL: API error: {e}")
        return 1

    text = "".join(getattr(b, "text", "") for b in msg.content)
    print(f"  response     : {text!r}")
    print(f"  input_tokens : {msg.usage.input_tokens}")
    print(f"  output_tokens: {msg.usage.output_tokens}")
    print(f"  stop_reason  : {msg.stop_reason}")
    print()
    if "true" in text.lower():
        print("RESULT: Anthropic auth works. Model reachable. Safe to run pair adjudication.")
        return 0
    print("WARN: model responded, but response shape was unexpected. Review and decide.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
