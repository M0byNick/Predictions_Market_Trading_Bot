import json
import logging
import time
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from arb_bot.config import load_config
from arb_bot.db import connect, init_schema, transaction

log = logging.getLogger(__name__)


def create_app() -> Flask:
    cfg = load_config()
    init_schema(cfg.db_path)

    templates = Path(__file__).parent / "templates"
    static_dir = Path(__file__).parent / "static"
    app = Flask(__name__, template_folder=str(templates), static_folder=str(static_dir))
    app.config["SECRET_KEY"] = "arb-bot-dashboard-dev"
    app.config["ARB_CFG"] = cfg

    def db():
        return connect(cfg.db_path)

    @app.get("/")
    def index():
        with db() as conn:
            counts = {
                "markets_kalshi": conn.execute(
                    "SELECT COUNT(*) FROM markets WHERE venue='kalshi' AND status='open'"
                ).fetchone()[0],
                "markets_poly_global": conn.execute(
                    "SELECT COUNT(*) FROM markets WHERE venue='poly_global' AND status='open'"
                ).fetchone()[0],
                "candidates_total": conn.execute("SELECT COUNT(*) FROM candidate_pairs").fetchone()[0],
                "verdicts_total": conn.execute("SELECT COUNT(*) FROM pair_verdicts").fetchone()[0],
                "approved_total": conn.execute(
                    "SELECT COUNT(*) FROM approved_pairs WHERE active=1"
                ).fetchone()[0],
                "approved_clean": conn.execute(
                    "SELECT COUNT(*) FROM approved_pairs WHERE active=1 AND tag='clean'"
                ).fetchone()[0],
                "approved_high_risk": conn.execute(
                    "SELECT COUNT(*) FROM approved_pairs WHERE active=1 AND tag='high_risk'"
                ).fetchone()[0],
                "rejected_total": conn.execute("SELECT COUNT(*) FROM rejected_pairs").fetchone()[0],
                "pending": conn.execute(
                    """
                    SELECT COUNT(*) FROM candidate_pairs c
                    JOIN pair_verdicts v ON v.candidate_id = c.id
                    LEFT JOIN approved_pairs a
                      ON a.kalshi_ticker=c.kalshi_ticker AND a.poly_global_market_id=c.poly_global_market_id
                    LEFT JOIN rejected_pairs r ON r.candidate_id=c.id
                    WHERE a.pair_id IS NULL AND r.candidate_id IS NULL
                    """
                ).fetchone()[0],
            }
        return render_template("index.html", counts=counts)

    @app.get("/queue")
    def queue():
        # Filter by tier:
        #   "safest"  = match=yes, risk in (none,low), no edge_case_flags
        #   "review"  = match=yes, risk in (none,low), HAS edge_case_flags (review carefully)
        #   "ambig"   = match=ambiguous
        #   "all"     = everything pending (default)
        tier = request.args.get("tier", "safest")
        with db() as conn:
            base = """
                FROM candidate_pairs c
                JOIN pair_verdicts v ON v.candidate_id = c.id
                LEFT JOIN approved_pairs a
                  ON a.kalshi_ticker = c.kalshi_ticker AND a.poly_global_market_id = c.poly_global_market_id
                LEFT JOIN rejected_pairs r ON r.candidate_id = c.id
                WHERE a.pair_id IS NULL AND r.candidate_id IS NULL
            """
            tier_clauses = {
                "safest": (
                    " AND v.match='yes' AND v.resolution_divergence_risk IN ('none','low')"
                    " AND (v.edge_case_flags IS NULL OR v.edge_case_flags='[]')"
                ),
                "review": (
                    " AND v.match='yes' AND v.resolution_divergence_risk IN ('none','low')"
                    " AND v.edge_case_flags IS NOT NULL AND v.edge_case_flags!='[]'"
                ),
                "ambig": " AND v.match='ambiguous'",
                "all": "",
            }
            order_by = (
                " ORDER BY"
                " CASE v.match WHEN 'yes' THEN 0 WHEN 'ambiguous' THEN 1 ELSE 2 END,"
                " CASE WHEN v.edge_case_flags IS NULL OR v.edge_case_flags='[]' THEN 0 ELSE 1 END,"
                " v.confidence DESC"
            )
            row = conn.execute(
                """SELECT c.id AS candidate_id, c.kalshi_ticker, c.poly_global_market_id,
                       c.cosine_similarity,
                       v.id AS verdict_id, v.match, v.confidence, v.resolution_aligned,
                       v.resolution_divergence_risk, v.match_polarity, v.divergence_reason,
                       v.normalized_question, v.reasoning,
                       v.edge_case_flags, v.edge_case_downgraded
                """ + base + tier_clauses.get(tier, "") + order_by + " LIMIT 1"
            ).fetchone()
            if not row:
                return render_template("queue_empty.html", tier=tier)
            kal = conn.execute(
                "SELECT * FROM markets WHERE venue='kalshi' AND venue_market_id=?",
                (row["kalshi_ticker"],),
            ).fetchone()
            poly = conn.execute(
                "SELECT * FROM markets WHERE venue='poly_global' AND venue_market_id=?",
                (row["poly_global_market_id"],),
            ).fetchone()
            # Counts per tier for the navigation
            tier_counts = {}
            for tname, clause in tier_clauses.items():
                if tname == "all":
                    sql = "SELECT COUNT(*) " + base
                else:
                    sql = "SELECT COUNT(*) " + base + clause
                tier_counts[tname] = conn.execute(sql).fetchone()[0]
            edge_flags = json.loads(row["edge_case_flags"]) if row["edge_case_flags"] else []
        return render_template(
            "queue.html",
            pair=row, kal=kal, poly=poly,
            tier=tier, tier_counts=tier_counts, edge_flags=edge_flags,
        )

    @app.post("/approve/<int:candidate_id>")
    def approve(candidate_id: int):
        tag = request.form.get("tag", "clean")
        if tag not in ("clean", "high_risk"):
            abort(400, "invalid tag")
        notes = request.form.get("notes", "")
        # Optional explicit override (radio buttons in queue.html); falls back
        # to the verdict's match_polarity if the form didn't send one.
        override_polarity = request.form.get("polarity")
        with db() as conn:
            cand = conn.execute(
                """
                SELECT c.*, v.resolution_divergence_risk, v.normalized_question,
                       v.match_polarity
                FROM candidate_pairs c
                JOIN pair_verdicts v ON v.candidate_id = c.id
                WHERE c.id = ?
                ORDER BY v.verdict_ts DESC LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
            if not cand:
                abort(404)
            polarity = (override_polarity or cand["match_polarity"] or "unknown").lower()
            if polarity not in ("same", "inverse", "unknown"):
                polarity = "unknown"
            pair_id = f"{cand['kalshi_ticker']}__{cand['poly_global_market_id']}"[:120]
            with transaction(conn):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO approved_pairs
                    (pair_id, kalshi_ticker, poly_global_market_id, normalized_question,
                     resolution_divergence_risk, match_polarity, tag, approved_by,
                     approved_ts, active, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        pair_id,
                        cand["kalshi_ticker"],
                        cand["poly_global_market_id"],
                        cand["normalized_question"],
                        cand["resolution_divergence_risk"],
                        polarity,
                        tag,
                        request.form.get("user", "nick"),
                        int(time.time()),
                        notes,
                    ),
                )
        flash(f"Approved {pair_id} as {tag} (polarity={polarity})", "success")
        return redirect(url_for("queue", tier=request.form.get("tier", "safest")))

    @app.post("/reject/<int:candidate_id>")
    def reject(candidate_id: int):
        reason = request.form.get("reason", "")
        with db() as conn:
            with transaction(conn):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO rejected_pairs
                    (candidate_id, rejected_by, rejected_ts, reason)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        request.form.get("user", "nick"),
                        int(time.time()),
                        reason,
                    ),
                )
        flash(f"Rejected candidate #{candidate_id}", "warning")
        return redirect(url_for("queue", tier=request.form.get("tier", "safest")))

    @app.get("/approved")
    def approved_list():
        tag = request.args.get("tag", "all")
        with db() as conn:
            query = """
                SELECT a.*, m1.title AS kalshi_title, m2.title AS poly_title
                FROM approved_pairs a
                LEFT JOIN markets m1 ON m1.venue='kalshi' AND m1.venue_market_id=a.kalshi_ticker
                LEFT JOIN markets m2 ON m2.venue='poly_global' AND m2.venue_market_id=a.poly_global_market_id
                WHERE a.active=1
            """
            params: tuple = ()
            if tag in ("clean", "high_risk"):
                query += " AND a.tag = ?"
                params = (tag,)
            query += " ORDER BY a.approved_ts DESC"
            rows = conn.execute(query, params).fetchall()
        return render_template("approved.html", rows=rows, tag=tag)

    @app.get("/rejected")
    def rejected_list():
        with db() as conn:
            rows = conn.execute(
                """
                SELECT r.*, c.kalshi_ticker, c.poly_global_market_id, c.cosine_similarity,
                       m1.title AS kalshi_title, m2.title AS poly_title
                FROM rejected_pairs r
                JOIN candidate_pairs c ON c.id = r.candidate_id
                LEFT JOIN markets m1 ON m1.venue='kalshi' AND m1.venue_market_id=c.kalshi_ticker
                LEFT JOIN markets m2 ON m2.venue='poly_global' AND m2.venue_market_id=c.poly_global_market_id
                ORDER BY r.rejected_ts DESC
                LIMIT 200
                """
            ).fetchall()
        return render_template("rejected.html", rows=rows)

    @app.get("/market/<venue>/<path:market_id>")
    def market_detail(venue: str, market_id: str):
        if venue not in ("kalshi", "poly_global"):
            abort(400)
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM markets WHERE venue=? AND venue_market_id=?",
                (venue, market_id),
            ).fetchone()
        if not row:
            abort(404)
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
        return render_template("market.html", row=row, raw=json.dumps(raw, indent=2))

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app()
    cfg = app.config["ARB_CFG"]
    app.run(host="0.0.0.0", port=cfg.dashboard_port, debug=False)


if __name__ == "__main__":
    main()
