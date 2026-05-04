import json
import logging
import time
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from arb_bot.config import load_config
from arb_bot.db import connect, init_schema, transaction


def _decision_snapshot(conn, candidate_id: int) -> tuple[int | None, str]:
    """Build a JSON snapshot of the latest verdict + relevant context.

    Returns (verdict_id, json_str). Used at approve/reject time so /learn
    can see exactly what the reviewer saw at the moment of the decision,
    even after later re-adjudications change the latest verdict.
    """
    row = conn.execute(
        """
        SELECT v.id AS verdict_id, v.match, v.confidence,
               v.resolution_aligned, v.resolution_divergence_risk,
               v.match_polarity, v.divergence_reason,
               v.normalized_question, v.reasoning, v.model,
               v.edge_case_flags, v.edge_case_downgraded,
               c.cosine_similarity, c.kalshi_ticker, c.poly_global_market_id,
               m1.title AS k_title, m2.title AS p_title
        FROM candidate_pairs c
        JOIN pair_verdicts v ON v.id = (
            SELECT id FROM pair_verdicts pv
            WHERE pv.candidate_id = c.id
            ORDER BY pv.verdict_ts DESC, pv.id DESC LIMIT 1
        )
        JOIN markets m1 ON m1.venue='kalshi' AND m1.venue_market_id=c.kalshi_ticker
        JOIN markets m2 ON m2.venue='poly_global' AND m2.venue_market_id=c.poly_global_market_id
        WHERE c.id = ?
        """,
        (candidate_id,),
    ).fetchone()
    if not row:
        return None, json.dumps({})
    flags = []
    try:
        if row["edge_case_flags"]:
            flags = [f.get("name") for f in json.loads(row["edge_case_flags"])]
    except Exception:
        pass
    snap = {
        "model": row["model"],
        "match": row["match"],
        "confidence": row["confidence"],
        "risk": row["resolution_divergence_risk"],
        "polarity": row["match_polarity"],
        "edge_case_flag_names": flags,
        "edge_case_downgraded": bool(row["edge_case_downgraded"]),
        "cosine_similarity": row["cosine_similarity"],
        "k_title": row["k_title"],
        "p_title": row["p_title"],
    }
    return row["verdict_id"], json.dumps(snap, ensure_ascii=False)

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
                # Pending = candidates with a verdict but not yet approved or
                # rejected. Joins on the LATEST verdict so we count each
                # candidate once even if it has multiple verdicts (Sonnet +
                # Opus, etc.). Limited to match=yes since match=no is
                # functionally rejected by the LLM and shouldn't appear in
                # human-review queues.
                "pending": conn.execute(
                    """
                    SELECT COUNT(*) FROM candidate_pairs c
                    JOIN pair_verdicts v ON v.id = (
                        SELECT id FROM pair_verdicts pv
                        WHERE pv.candidate_id = c.id
                        ORDER BY pv.verdict_ts DESC, pv.id DESC LIMIT 1
                    )
                    LEFT JOIN approved_pairs a
                      ON a.kalshi_ticker=c.kalshi_ticker AND a.poly_global_market_id=c.poly_global_market_id
                    LEFT JOIN rejected_pairs r ON r.candidate_id=c.id
                    WHERE a.pair_id IS NULL AND r.candidate_id IS NULL
                      AND v.match IN ('yes', 'ambiguous')
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
        # Uses LATEST verdict per candidate (across all models) so Opus
        # verdicts override earlier Sonnet ones where present.
        #
        # ?focus=<candidate_id> overrides tier filtering and shows that
        # exact candidate (used by the bulk-review "→ details" link).
        tier = request.args.get("tier", "safest")
        focus_id = request.args.get("focus", type=int)
        with db() as conn:
            base = """
                FROM candidate_pairs c
                JOIN pair_verdicts v ON v.id = (
                    SELECT id FROM pair_verdicts pv
                    WHERE pv.candidate_id = c.id
                    ORDER BY pv.verdict_ts DESC, pv.id DESC LIMIT 1
                )
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
            select_cols = """SELECT c.id AS candidate_id, c.kalshi_ticker, c.poly_global_market_id,
                       c.cosine_similarity,
                       v.id AS verdict_id, v.match, v.confidence, v.resolution_aligned,
                       v.resolution_divergence_risk, v.match_polarity, v.divergence_reason,
                       v.normalized_question, v.reasoning,
                       v.edge_case_flags, v.edge_case_downgraded
                """
            if focus_id is not None:
                # Honor ?focus=<candidate_id>: bypass tier filter and load the
                # exact candidate. Still excludes already-approved/rejected so
                # operators don't accidentally re-decide via direct link.
                row = conn.execute(
                    select_cols + base + " AND c.id = ?",
                    (focus_id,),
                ).fetchone()
                if row is None:
                    # Fallback: candidate may have been just-approved/rejected.
                    # Show without the approved/rejected filter so the operator
                    # at least sees what they clicked on.
                    row = conn.execute(
                        select_cols + """
                        FROM candidate_pairs c
                        JOIN pair_verdicts v ON v.id = (
                            SELECT id FROM pair_verdicts pv
                            WHERE pv.candidate_id = c.id
                            ORDER BY pv.verdict_ts DESC, pv.id DESC LIMIT 1
                        )
                        WHERE c.id = ?
                        """,
                        (focus_id,),
                    ).fetchone()
                    if row is not None:
                        flash(
                            f"Candidate #{focus_id} has already been "
                            f"approved or rejected; showing read-only.",
                            "warning",
                        )
            else:
                row = conn.execute(
                    select_cols + base + tier_clauses.get(tier, "") + order_by + " LIMIT 1"
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
                JOIN pair_verdicts v ON v.id = (
                    SELECT id FROM pair_verdicts pv
                    WHERE pv.candidate_id = c.id
                    ORDER BY pv.verdict_ts DESC, pv.id DESC LIMIT 1
                )
                WHERE c.id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if not cand:
                abort(404)
            polarity = (override_polarity or cand["match_polarity"] or "unknown").lower()
            if polarity not in ("same", "inverse", "unknown"):
                polarity = "unknown"
            pair_id = f"{cand['kalshi_ticker']}__{cand['poly_global_market_id']}"[:120]
            verdict_id, ctx = _decision_snapshot(conn, candidate_id)
            with transaction(conn):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO approved_pairs
                    (pair_id, kalshi_ticker, poly_global_market_id, normalized_question,
                     resolution_divergence_risk, match_polarity, tag, approved_by,
                     approved_ts, active, notes, verdict_id, decision_context)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
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
                        verdict_id,
                        ctx,
                    ),
                )
        flash(f"Approved {pair_id} as {tag} (polarity={polarity})", "success")
        return redirect(url_for("queue", tier=request.form.get("tier", "safest")))

    @app.post("/reject/<int:candidate_id>")
    def reject(candidate_id: int):
        reason = request.form.get("reason", "")
        with db() as conn:
            verdict_id, ctx = _decision_snapshot(conn, candidate_id)
            with transaction(conn):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO rejected_pairs
                    (candidate_id, rejected_by, rejected_ts, reason,
                     verdict_id, decision_context)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        request.form.get("user", "nick"),
                        int(time.time()),
                        reason,
                        verdict_id,
                        ctx,
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

    @app.get("/queue/list")
    def queue_list():
        """Bulk-review list view: many pairs at once with inline approve/reject.

        Useful for the review-recommended tier where many pairs share an
        edge_case_pattern and a reviewer can fly through them once they
        understand the pattern.
        """
        tier = request.args.get("tier", "review")
        flag_filter = request.args.get("flag")  # optional edge_case name filter
        page = int(request.args.get("page", "1"))
        per_page = int(request.args.get("per_page", "30"))

        with db() as conn:
            base = """
                FROM candidate_pairs c
                JOIN pair_verdicts v ON v.id = (
                    SELECT id FROM pair_verdicts pv
                    WHERE pv.candidate_id = c.id
                    ORDER BY pv.verdict_ts DESC, pv.id DESC LIMIT 1
                )
                JOIN markets m1 ON m1.venue='kalshi' AND m1.venue_market_id=c.kalshi_ticker
                JOIN markets m2 ON m2.venue='poly_global' AND m2.venue_market_id=c.poly_global_market_id
                LEFT JOIN approved_pairs a
                  ON a.kalshi_ticker = c.kalshi_ticker AND a.poly_global_market_id = c.poly_global_market_id
                LEFT JOIN rejected_pairs r ON r.candidate_id = c.id
                WHERE a.pair_id IS NULL AND r.candidate_id IS NULL
            """
            tier_clauses = {
                "safest": " AND v.match='yes' AND v.resolution_divergence_risk IN ('none','low')"
                          " AND (v.edge_case_flags IS NULL OR v.edge_case_flags='[]')",
                "review": " AND v.match='yes' AND v.resolution_divergence_risk IN ('none','low')"
                          " AND v.edge_case_flags IS NOT NULL AND v.edge_case_flags!='[]'",
                "ambig": " AND v.match='ambiguous'",
                "all": " AND v.match IN ('yes','ambiguous')",
            }
            params: list = []
            sql = """
                SELECT c.id AS candidate_id, c.kalshi_ticker, c.poly_global_market_id,
                       c.cosine_similarity,
                       v.match, v.confidence, v.resolution_divergence_risk,
                       v.match_polarity, v.edge_case_flags, v.normalized_question,
                       v.divergence_reason, v.reasoning,
                       m1.title AS k_title, m2.title AS p_title,
                       m1.yes_bid AS k_yes_bid, m1.yes_ask AS k_yes_ask,
                       m2.yes_bid AS p_yes_bid, m2.yes_ask AS p_yes_ask
            """ + base + tier_clauses.get(tier, "")
            if flag_filter:
                # Substring match on the JSON-encoded edge_case_flags
                sql += " AND v.edge_case_flags LIKE ?"
                params.append(f'%"{flag_filter}"%')
            sql += " ORDER BY v.confidence DESC, c.cosine_similarity DESC"
            sql += " LIMIT ? OFFSET ?"
            params.extend([per_page, (page - 1) * per_page])
            rows = conn.execute(sql, params).fetchall()

            # Count total for pagination
            count_sql = "SELECT COUNT(*) " + base + tier_clauses.get(tier, "")
            count_params: list = []
            if flag_filter:
                count_sql += " AND v.edge_case_flags LIKE ?"
                count_params.append(f'%"{flag_filter}"%')
            total = conn.execute(count_sql, count_params).fetchone()[0]

            # Flag-name distribution (only for review tier — most useful there)
            flag_distribution: list[tuple[str, int]] = []
            if tier == "review":
                # JSON parsing in SQLite is slow; do it in Python
                flag_count: dict[str, int] = {}
                for r in conn.execute(
                    "SELECT v.edge_case_flags " + base + tier_clauses["review"]
                ):
                    flags = json.loads(r["edge_case_flags"]) if r["edge_case_flags"] else []
                    for f in flags:
                        flag_count[f["name"]] = flag_count.get(f["name"], 0) + 1
                flag_distribution = sorted(flag_count.items(), key=lambda x: -x[1])

        # Hydrate edge_case_flags JSON for display
        rows_h = []
        for r in rows:
            d = dict(r)
            d["edge_case_flags_parsed"] = (
                json.loads(d["edge_case_flags"]) if d["edge_case_flags"] else []
            )
            rows_h.append(d)

        return render_template(
            "queue_list.html",
            rows=rows_h,
            tier=tier,
            flag_filter=flag_filter,
            flag_distribution=flag_distribution,
            page=page,
            per_page=per_page,
            total=total,
            total_pages=(total + per_page - 1) // per_page,
        )

    @app.post("/bulk_action")
    def bulk_action():
        """Bulk approve/reject — applies to candidate_ids passed as a comma-
        separated form field. Used by the queue_list page checkbox flow.
        """
        action = request.form.get("action", "")
        ids_raw = request.form.get("candidate_ids", "")
        tag = request.form.get("tag", "clean")
        return_tier = request.form.get("tier", "review")
        return_flag = request.form.get("flag", "")
        return_page = request.form.get("page", "1")

        if action not in ("approve", "reject"):
            abort(400, "invalid action")
        ids: list[int] = []
        for tok in ids_raw.split(","):
            tok = tok.strip()
            if tok.isdigit():
                ids.append(int(tok))
        if not ids:
            flash("No pairs selected.", "warning")
            return redirect(url_for("queue_list", tier=return_tier,
                                    flag=return_flag, page=return_page))

        now_ts = int(time.time())
        actor = request.form.get("user", "bulk-form")

        with db() as conn:
            with transaction(conn):
                if action == "approve":
                    if tag not in ("clean", "high_risk"):
                        abort(400, "invalid tag")
                    for cand_id in ids:
                        cand = conn.execute(
                            """
                            SELECT c.*, v.resolution_divergence_risk,
                                   v.normalized_question, v.match_polarity
                            FROM candidate_pairs c
                            JOIN pair_verdicts v ON v.id = (
                                SELECT id FROM pair_verdicts pv
                                WHERE pv.candidate_id = c.id
                                ORDER BY pv.verdict_ts DESC, pv.id DESC LIMIT 1
                            )
                            WHERE c.id = ?
                            """,
                            (cand_id,),
                        ).fetchone()
                        if not cand:
                            continue
                        polarity = (cand["match_polarity"] or "unknown").lower()
                        if polarity not in ("same", "inverse", "unknown"):
                            polarity = "unknown"
                        pair_id = f"{cand['kalshi_ticker']}__{cand['poly_global_market_id']}"[:120]
                        verdict_id, ctx = _decision_snapshot(conn, cand_id)
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO approved_pairs
                            (pair_id, kalshi_ticker, poly_global_market_id,
                             normalized_question, resolution_divergence_risk,
                             match_polarity, tag, approved_by, approved_ts,
                             active, notes, verdict_id, decision_context)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                            """,
                            (
                                pair_id, cand["kalshi_ticker"],
                                cand["poly_global_market_id"],
                                cand["normalized_question"],
                                cand["resolution_divergence_risk"],
                                polarity, tag, actor, now_ts,
                                f"bulk-approved from {return_tier} tier",
                                verdict_id, ctx,
                            ),
                        )
                else:  # reject
                    reason = request.form.get("reason", "bulk-reject")
                    for cand_id in ids:
                        verdict_id, ctx = _decision_snapshot(conn, cand_id)
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO rejected_pairs
                            (candidate_id, rejected_by, rejected_ts, reason,
                             verdict_id, decision_context)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (cand_id, actor, now_ts, reason, verdict_id, ctx),
                        )

        flash(f"{action.title()}d {len(ids)} pair(s)", "success")
        return redirect(url_for("queue_list", tier=return_tier,
                                flag=return_flag, page=return_page))

    @app.get("/stats")
    def stats():
        """Approval-rate-over-time + per-model + per-tier accuracy."""
        with db() as conn:
            # Daily rollup of approve / reject events (UTC)
            daily = conn.execute("""
                WITH events AS (
                    SELECT date(approved_ts, 'unixepoch') AS day,
                           tag, 'approved' AS action,
                           approved_by AS actor
                    FROM approved_pairs WHERE active = 1
                    UNION ALL
                    SELECT date(rejected_ts, 'unixepoch') AS day,
                           NULL AS tag, 'rejected' AS action,
                           rejected_by AS actor
                    FROM rejected_pairs
                )
                SELECT day, action, tag, COUNT(*) AS n
                FROM events
                GROUP BY day, action, tag
                ORDER BY day DESC
            """).fetchall()

            # Aggregate: by-day approvals / rejections / clean-vs-high
            by_day: dict[str, dict[str, int]] = {}
            for r in daily:
                d = r["day"] or "(unknown)"
                if d not in by_day:
                    by_day[d] = {
                        "approved_clean": 0, "approved_high_risk": 0,
                        "rejected": 0, "total": 0,
                    }
                if r["action"] == "approved":
                    if r["tag"] == "clean":
                        by_day[d]["approved_clean"] += r["n"]
                    else:
                        by_day[d]["approved_high_risk"] += r["n"]
                else:
                    by_day[d]["rejected"] += r["n"]
                by_day[d]["total"] += r["n"]
            day_rows = sorted(by_day.items(), reverse=True)

            # Approval rate by source LLM model (which model surfaced the verdict?)
            by_model = conn.execute("""
                WITH latest_verdict AS (
                    SELECT v.candidate_id, v.model
                    FROM pair_verdicts v
                    JOIN (
                        SELECT candidate_id, MAX(verdict_ts) AS ts
                        FROM pair_verdicts GROUP BY candidate_id
                    ) t ON t.candidate_id = v.candidate_id AND t.ts = v.verdict_ts
                ),
                outcomes AS (
                    SELECT lv.candidate_id, lv.model,
                        CASE
                            WHEN a.pair_id IS NOT NULL THEN 'approved'
                            WHEN r.candidate_id IS NOT NULL THEN 'rejected'
                            ELSE 'pending'
                        END AS outcome
                    FROM latest_verdict lv
                    JOIN candidate_pairs c ON c.id = lv.candidate_id
                    LEFT JOIN approved_pairs a
                      ON a.kalshi_ticker = c.kalshi_ticker
                     AND a.poly_global_market_id = c.poly_global_market_id
                    LEFT JOIN rejected_pairs r ON r.candidate_id = lv.candidate_id
                )
                SELECT model,
                    COUNT(*) AS total,
                    SUM(CASE WHEN outcome='approved' THEN 1 ELSE 0 END) AS approved,
                    SUM(CASE WHEN outcome='rejected' THEN 1 ELSE 0 END) AS rejected,
                    SUM(CASE WHEN outcome='pending'  THEN 1 ELSE 0 END) AS pending
                FROM outcomes
                GROUP BY model
                ORDER BY total DESC
            """).fetchall()

            # Latest activity (last 30 actions across approve+reject)
            recent = conn.execute("""
                SELECT * FROM (
                    SELECT 'approved' AS action, approved_ts AS ts,
                           pair_id AS subject, tag, approved_by AS actor
                    FROM approved_pairs WHERE active = 1
                    UNION ALL
                    SELECT 'rejected' AS action, rejected_ts AS ts,
                           CAST(candidate_id AS TEXT) AS subject,
                           NULL AS tag, rejected_by AS actor
                    FROM rejected_pairs
                )
                ORDER BY ts DESC LIMIT 30
            """).fetchall()

            # Headline numbers
            total_candidates = conn.execute(
                "SELECT COUNT(*) FROM candidate_pairs"
            ).fetchone()[0]
            total_approved = conn.execute(
                "SELECT COUNT(*) FROM approved_pairs WHERE active=1"
            ).fetchone()[0]
            total_rejected = conn.execute(
                "SELECT COUNT(*) FROM rejected_pairs"
            ).fetchone()[0]
            decided = total_approved + total_rejected
            approval_rate = total_approved / max(1, decided)

        return render_template(
            "stats.html",
            day_rows=day_rows,
            by_model=by_model,
            recent=recent,
            total_candidates=total_candidates,
            total_approved=total_approved,
            total_rejected=total_rejected,
            approval_rate=approval_rate,
        )

    @app.get("/learn")
    def learn():
        """Pattern analysis: what attributes correlate with approve vs reject?"""
        with db() as conn:
            # Pull every decision (approve + reject) with its snapshot
            decisions = []
            for r in conn.execute(
                "SELECT 'approve' AS outcome, tag, decision_context, approved_ts AS ts "
                "FROM approved_pairs WHERE active=1 AND decision_context IS NOT NULL"
            ):
                decisions.append((r["outcome"], r["tag"], r["decision_context"], r["ts"]))
            for r in conn.execute(
                "SELECT 'reject' AS outcome, NULL AS tag, decision_context, rejected_ts AS ts "
                "FROM rejected_pairs WHERE decision_context IS NOT NULL"
            ):
                decisions.append((r["outcome"], r["tag"], r["decision_context"], r["ts"]))

        # Aggregate by attribute
        from collections import defaultdict
        # (attr_value) -> {approve, reject}
        by_flag: dict[str, dict[str, int]] = defaultdict(lambda: {"approve": 0, "reject": 0})
        by_risk: dict[str, dict[str, int]] = defaultdict(lambda: {"approve": 0, "reject": 0})
        by_polarity: dict[str, dict[str, int]] = defaultdict(lambda: {"approve": 0, "reject": 0})
        by_match: dict[str, dict[str, int]] = defaultdict(lambda: {"approve": 0, "reject": 0})
        by_model: dict[str, dict[str, int]] = defaultdict(lambda: {"approve": 0, "reject": 0})
        by_cosine_bucket: dict[str, dict[str, int]] = defaultdict(lambda: {"approve": 0, "reject": 0})
        by_confidence_bucket: dict[str, dict[str, int]] = defaultdict(lambda: {"approve": 0, "reject": 0})
        by_tag: dict[str, dict[str, int]] = defaultdict(lambda: {"approve": 0, "reject": 0})

        n_approved = 0
        n_rejected = 0
        for outcome, tag, ctx_json, _ts in decisions:
            try:
                ctx = json.loads(ctx_json or "{}")
            except Exception:
                continue
            if outcome == "approve":
                n_approved += 1
                if tag:
                    by_tag[tag][outcome] += 1
            else:
                n_rejected += 1
            risk = ctx.get("risk", "?") or "?"
            polarity = ctx.get("polarity", "?") or "?"
            match = ctx.get("match", "?") or "?"
            model = ctx.get("model", "?") or "?"
            conf = ctx.get("confidence")
            cos = ctx.get("cosine_similarity")
            flags = ctx.get("edge_case_flag_names") or []
            by_risk[risk][outcome] += 1
            by_polarity[polarity][outcome] += 1
            by_match[match][outcome] += 1
            by_model[model][outcome] += 1
            if conf is not None:
                bucket = f"{int(conf * 10) * 10}-{int(conf * 10) * 10 + 10}%"
                by_confidence_bucket[bucket][outcome] += 1
            if cos is not None:
                bucket = f"{int(cos * 20) / 20:.2f}-{int(cos * 20) / 20 + 0.05:.2f}"
                by_cosine_bucket[bucket][outcome] += 1
            if not flags:
                by_flag["(none)"][outcome] += 1
            else:
                for f in flags:
                    by_flag[f][outcome] += 1

        def _to_table(d: dict) -> list[dict]:
            out = []
            for k, v in d.items():
                total = v["approve"] + v["reject"]
                rate = v["approve"] / total if total else 0
                out.append({
                    "key": k,
                    "approve": v["approve"],
                    "reject": v["reject"],
                    "total": total,
                    "approval_rate": rate,
                })
            return sorted(out, key=lambda x: -x["total"])

        return render_template(
            "learn.html",
            n_approved=n_approved,
            n_rejected=n_rejected,
            by_flag=_to_table(by_flag),
            by_risk=_to_table(by_risk),
            by_polarity=_to_table(by_polarity),
            by_match=_to_table(by_match),
            by_model=_to_table(by_model),
            by_cosine_bucket=sorted(_to_table(by_cosine_bucket), key=lambda x: x["key"]),
            by_confidence_bucket=sorted(_to_table(by_confidence_bucket), key=lambda x: x["key"]),
            by_tag=_to_table(by_tag),
        )

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
