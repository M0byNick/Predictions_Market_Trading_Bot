import logging
import sqlite3
import time
from typing import Iterable

import numpy as np

from arb_bot.config import Config
from arb_bot.db import transaction

log = logging.getLogger(__name__)

_MODEL = None


def _get_model(name: str):
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer

        log.info("Loading embedding model: %s", name)
        _MODEL = SentenceTransformer(name)
    return _MODEL


def _text_for_market(row: sqlite3.Row) -> str:
    parts = [row["title"] or ""]
    if row["description"]:
        parts.append(row["description"])
    if row["resolution_criteria"]:
        parts.append(f"Resolution: {row['resolution_criteria']}")
    return " | ".join(p.strip() for p in parts if p.strip())


def _pack(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def _unpack(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def compute_missing(conn: sqlite3.Connection, cfg: Config) -> int:
    model = _get_model(cfg.embed_model)
    now_ts = int(time.time())
    rows = conn.execute(
        """
        SELECT m.venue, m.venue_market_id, m.title, m.description, m.resolution_criteria
        FROM markets m
        LEFT JOIN market_embeddings e
          ON e.venue = m.venue AND e.venue_market_id = m.venue_market_id AND e.model = ?
        WHERE m.status = 'open' AND e.venue IS NULL
        """,
        (cfg.embed_model,),
    ).fetchall()
    if not rows:
        return 0

    texts = [_text_for_market(r) for r in rows]
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    with transaction(conn):
        for r, v in zip(rows, vectors, strict=True):
            conn.execute(
                """
                INSERT OR REPLACE INTO market_embeddings
                (venue, venue_market_id, model, embedding, computed_ts)
                VALUES (?, ?, ?, ?, ?)
                """,
                (r["venue"], r["venue_market_id"], cfg.embed_model, _pack(v), now_ts),
            )
    log.info("Embeddings: computed %d new", len(rows))
    return len(rows)


def _load_vectors(
    conn: sqlite3.Connection, venue: str, model: str
) -> tuple[list[str], np.ndarray]:
    rows = conn.execute(
        """
        SELECT venue_market_id, embedding FROM market_embeddings
        WHERE venue = ? AND model = ?
        """,
        (venue, model),
    ).fetchall()
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    ids = [r["venue_market_id"] for r in rows]
    mat = np.stack([_unpack(r["embedding"]) for r in rows])
    return ids, mat


def generate_candidates(conn: sqlite3.Connection, cfg: Config) -> int:
    kalshi_ids, kalshi_mat = _load_vectors(conn, "kalshi", cfg.embed_model)
    poly_ids, poly_mat = _load_vectors(conn, "poly_us", cfg.embed_model)
    if not kalshi_ids or not poly_ids:
        log.info("generate_candidates: empty side (kalshi=%d, poly_us=%d)", len(kalshi_ids), len(poly_ids))
        return 0

    # Both matrices are L2-normalized by SentenceTransformer, so dot = cosine
    sim = kalshi_mat @ poly_mat.T  # shape: (K, P)
    top_k = min(cfg.candidate_top_k, len(poly_ids))
    now_ts = int(time.time())
    inserted = 0

    with transaction(conn):
        for i, kal in enumerate(kalshi_ids):
            # top-K indices into poly
            idxs = np.argpartition(-sim[i], top_k - 1)[:top_k]
            for j in idxs:
                score = float(sim[i, j])
                if score < cfg.embed_cosine_threshold:
                    continue
                poly = poly_ids[j]
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO candidate_pairs
                    (kalshi_ticker, poly_us_market_id, cosine_similarity, generated_ts)
                    VALUES (?, ?, ?, ?)
                    """,
                    (kal, poly, score, now_ts),
                )
                if cur.rowcount > 0:
                    inserted += 1
    log.info("Candidates: inserted %d new pairs", inserted)
    return inserted


def pending_candidates(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.id, c.kalshi_ticker, c.poly_us_market_id, c.cosine_similarity
        FROM candidate_pairs c
        LEFT JOIN pair_verdicts v ON v.candidate_id = c.id
        LEFT JOIN approved_pairs a
          ON a.kalshi_ticker = c.kalshi_ticker AND a.poly_us_market_id = c.poly_us_market_id
        LEFT JOIN rejected_pairs r ON r.candidate_id = c.id
        WHERE v.id IS NULL AND a.pair_id IS NULL AND r.candidate_id IS NULL
        ORDER BY c.cosine_similarity DESC
        """
    ).fetchall()
