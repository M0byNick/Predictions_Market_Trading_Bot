import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# override=True so the .env is authoritative over any pre-existing shell
# environment variables (e.g. an empty ANTHROPIC_API_KEY left by the dev
# harness). This is the right convention for a bot that owns its own auth.
_DOTENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_DOTENV_PATH, override=True)


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v or ""


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    # Runtime
    mode: str
    data_dir: Path
    heartbeat_path: Path
    ingest_interval_sec: int
    dashboard_port: int

    # Kalshi
    kalshi_access_key: str
    kalshi_private_key_path: Path
    kalshi_base_url: str

    # Polymarket Global (primary venue for non-US users — Ireland/EU/etc.)
    # No auth needed for read-only ingest. Wallet only required for live
    # trading; for paper v1 we leave poly_wallet_private_key empty.
    poly_global_gamma_url: str
    poly_global_clob_url: str
    poly_global_rate_per_sec: float
    poly_wallet_private_key: str  # Polygon EIP-712 key for live trading; empty in paper

    # Anthropic
    anthropic_api_key: str
    anthropic_model: str

    # Risk (paper)
    paper_max_position_usd: float
    paper_daily_max_loss_usd: float
    paper_min_edge_bps: int

    # Mapping
    embed_model: str
    embed_cosine_threshold: float
    candidate_top_k: int

    # Ingest filters (kept tight by default — Kalshi has 750K+ active
    # markets, most of which are stale/never-traded; we want only the
    # liquid ones for arb mapping + signal detection).
    kalshi_min_volume: float
    poly_global_min_volume: float
    store_raw_json: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "arb_bot.sqlite"


def load_config() -> Config:
    # Anchor relative paths to the Arb_Bot package root, not the shell CWD.
    # Resolves /Users/.../Arb_Bot regardless of where the script is invoked from.
    _ARB_ROOT = Path(__file__).resolve().parents[2]
    _RUNNING_IN_DOCKER = Path("/app/secrets").exists() or Path("/.dockerenv").exists()

    def _resolve(p: str) -> Path:
        path = Path(p)
        # Docker paths (/app/...) sentinel: when developing on host (not in
        # the container), automatically remap /app/* to <ARB_ROOT>/*. Same
        # .env works in both environments.
        if not _RUNNING_IN_DOCKER and str(path).startswith("/app/"):
            return (_ARB_ROOT / str(path)[len("/app/"):]).resolve()
        if path.is_absolute():
            return path
        return (_ARB_ROOT / path).resolve()

    data_dir = _resolve(_env("DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Config(
        mode=_env("MODE", "paper"),
        data_dir=data_dir,
        heartbeat_path=_resolve(_env("HEARTBEAT_PATH", str(data_dir / ".heartbeat"))),
        ingest_interval_sec=_env_int("INGEST_INTERVAL_SEC", 60),
        dashboard_port=_env_int("DASHBOARD_PORT", 8090),
        kalshi_access_key=_env("KALSHI_ACCESS_KEY"),
        kalshi_private_key_path=_resolve(_env("KALSHI_PRIVATE_KEY_PATH", "./secrets/kalshi.pem")),
        kalshi_base_url=_env("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"),
        poly_global_gamma_url=_env("POLY_GLOBAL_GAMMA_URL", "https://gamma-api.polymarket.com"),
        poly_global_clob_url=_env("POLY_GLOBAL_CLOB_URL", "https://clob.polymarket.com"),
        poly_global_rate_per_sec=_env_float("POLY_GLOBAL_RATE_PER_SEC", 8.0),
        poly_wallet_private_key=_env("POLY_WALLET_PRIVATE_KEY"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        paper_max_position_usd=_env_float("PAPER_MAX_POSITION_USD", 500.0),
        paper_daily_max_loss_usd=_env_float("PAPER_DAILY_MAX_LOSS_USD", 100.0),
        paper_min_edge_bps=_env_int("PAPER_MIN_EDGE_BPS", 200),
        embed_model=_env("EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
        embed_cosine_threshold=_env_float("EMBED_COSINE_THRESHOLD", 0.75),
        candidate_top_k=_env_int("CANDIDATE_TOP_K", 5),
        kalshi_min_volume=_env_float("KALSHI_MIN_VOLUME", 10000.0),
        poly_global_min_volume=_env_float("POLY_GLOBAL_MIN_VOLUME", 10000.0),
        store_raw_json=_env("STORE_RAW_JSON", "false").lower() in ("1", "true", "yes"),
    )
