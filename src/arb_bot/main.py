import logging
import time

from arb_bot.config import load_config
from arb_bot.db import connect, init_schema
from arb_bot.executor import paper as paper_exec
from arb_bot.heartbeat import touch
from arb_bot.ingest import kalshi as kalshi_ingest
from arb_bot.ingest import polymarket_global as poly_ingest
from arb_bot.mapping import embeddings as emb
from arb_bot.risk import limits as risk
from arb_bot.signal import spread as sig

log = logging.getLogger(__name__)


def cycle() -> None:
    cfg = load_config()
    init_schema(cfg.db_path)
    with connect(cfg.db_path) as conn:
        try:
            kalshi_ingest.upsert_markets(conn, cfg)
        except Exception:
            log.exception("Kalshi ingest failed")
        try:
            poly_ingest.upsert_markets(conn, cfg)
        except Exception:
            log.exception("Polymarket Global ingest failed")

        try:
            emb.compute_missing(conn, cfg)
            emb.generate_candidates(conn, cfg)
        except Exception:
            log.exception("Embedding / candidate gen failed")

        # Signal scan against approved pairs
        try:
            signals = sig.scan_all(conn, cfg)
            for s in signals:
                sig_id = sig.record_signal(conn, s)
                if not s.would_trade:
                    continue
                ok, reason = risk.check(conn, cfg, s.pair_id)
                if not ok:
                    log.warning("Risk block for %s: %s", s.pair_id, reason)
                    continue
                paper_exec.simulate_fill(conn, sig_id, s)
        except Exception:
            log.exception("Signal / execution step failed")

    touch(cfg.heartbeat_path)


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    log.info(
        "Arb_Bot starting: mode=%s db=%s interval=%ds",
        cfg.mode, cfg.db_path, cfg.ingest_interval_sec,
    )
    while True:
        t0 = time.time()
        try:
            cycle()
        except Exception:
            log.exception("Unhandled cycle error")
        elapsed = time.time() - t0
        sleep_for = max(cfg.ingest_interval_sec - elapsed, 5.0)
        log.info("Cycle done in %.1fs; sleeping %.1fs", elapsed, sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    run()
