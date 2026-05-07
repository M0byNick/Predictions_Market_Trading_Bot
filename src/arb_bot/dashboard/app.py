import json
import logging
import time
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

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
        # active=1 (default), 0 = inactive only, -1 (or "any") = both
        status = request.args.get("status", "active")
        with db() as conn:
            query = """
                SELECT a.*, m1.title AS kalshi_title, m2.title AS poly_title
                FROM approved_pairs a
                LEFT JOIN markets m1 ON m1.venue='kalshi' AND m1.venue_market_id=a.kalshi_ticker
                LEFT JOIN markets m2 ON m2.venue='poly_global' AND m2.venue_market_id=a.poly_global_market_id
                WHERE 1=1
            """
            params: list = []
            if status == "active":
                query += " AND a.active=1"
            elif status == "inactive":
                query += " AND a.active=0"
            # else "any" → no filter
            if tag in ("clean", "high_risk"):
                query += " AND a.tag = ?"
                params.append(tag)
            query += " ORDER BY a.approved_ts DESC"
            rows = conn.execute(query, params).fetchall()
            # Count summary for the filter chips
            counts = {
                "active": conn.execute(
                    "SELECT COUNT(*) FROM approved_pairs WHERE active=1"
                ).fetchone()[0],
                "inactive": conn.execute(
                    "SELECT COUNT(*) FROM approved_pairs WHERE active=0"
                ).fetchone()[0],
            }
        return render_template(
            "approved.html",
            rows=rows, tag=tag, status=status, counts=counts,
        )

    @app.post("/deactivate/<path:pair_id>")
    def deactivate_pair(pair_id: str):
        """Mark an approved pair as inactive — bot stops scanning it for
        signals but the row + decision_context are preserved for /learn.
        Use this for data-quality issues (stale prices, mis-tagged
        polarity) without losing the audit trail.
        """
        reason = request.form.get("reason", "")
        with db() as conn:
            cur = conn.execute(
                "SELECT pair_id, notes FROM approved_pairs WHERE pair_id=?",
                (pair_id,),
            ).fetchone()
            if not cur:
                abort(404)
            with transaction(conn):
                # Append deactivation reason to notes (keeps history readable)
                new_notes = cur["notes"] or ""
                if reason:
                    sep = " | " if new_notes else ""
                    new_notes = f"{new_notes}{sep}DEACTIVATED({int(time.time())}): {reason}"
                conn.execute(
                    "UPDATE approved_pairs SET active=0, notes=? WHERE pair_id=?",
                    (new_notes, pair_id),
                )
        flash(f"Deactivated {pair_id}", "warning")
        # Honor an explicit return_to so the deactivate button can be
        # invoked from /pnl, /dry_run, etc. and bring the user back to
        # the page they were on, instead of always sending them to
        # /approved.
        return_to = request.form.get("return_to")
        if return_to in ("pnl", "dry_run", "ws_status"):
            return redirect(url_for(return_to))
        return redirect(url_for(
            "approved_list",
            tag=request.form.get("tag", "all"),
            status=request.form.get("status", "active"),
        ))

    @app.post("/bulk_deactivate")
    def bulk_deactivate():
        """Bulk-mark approved pairs as inactive.

        Used by /dry_run to cull data-quality fakes (stale prices, mis-
        tagged polarity, settled-but-cached markets) in one click after
        the operator inspects the scan.
        """
        ids_raw = request.form.get("pair_ids", "")
        reason = request.form.get("reason", "bulk-deactivate from /dry_run")
        pair_ids = [tok for tok in (s.strip() for s in ids_raw.split(",")) if tok]
        if not pair_ids:
            flash("No pairs selected.", "warning")
            return redirect(url_for("dry_run"))

        now_ts = int(time.time())
        n_done = 0
        with db() as conn:
            with transaction(conn):
                for pid in pair_ids:
                    cur = conn.execute(
                        "SELECT pair_id, notes, active FROM approved_pairs WHERE pair_id=?",
                        (pid,),
                    ).fetchone()
                    if not cur or cur["active"] == 0:
                        continue
                    new_notes = cur["notes"] or ""
                    sep = " | " if new_notes else ""
                    new_notes = f"{new_notes}{sep}DEACTIVATED({now_ts}): {reason}"
                    conn.execute(
                        "UPDATE approved_pairs SET active=0, notes=? WHERE pair_id=?",
                        (new_notes, pid),
                    )
                    n_done += 1
        flash(f"Deactivated {n_done} pair(s).", "warning")
        return redirect(request.form.get("return_to") or url_for("dry_run"))

    @app.post("/reactivate/<path:pair_id>")
    def reactivate_pair(pair_id: str):
        """Re-enable a previously-deactivated pair. Bot resumes scanning."""
        with db() as conn:
            cur = conn.execute(
                "SELECT pair_id, notes FROM approved_pairs WHERE pair_id=?",
                (pair_id,),
            ).fetchone()
            if not cur:
                abort(404)
            with transaction(conn):
                new_notes = cur["notes"] or ""
                sep = " | " if new_notes else ""
                new_notes = f"{new_notes}{sep}REACTIVATED({int(time.time())})"
                conn.execute(
                    "UPDATE approved_pairs SET active=1, notes=? WHERE pair_id=?",
                    (new_notes, pair_id),
                )
        flash(f"Reactivated {pair_id}", "success")
        return redirect(url_for(
            "approved_list",
            tag=request.form.get("tag", "all"),
            status=request.form.get("status", "inactive"),
        ))

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
                       m1.description AS k_desc, m2.description AS p_desc,
                       m1.resolution_criteria AS k_rules,
                       m2.resolution_criteria AS p_rules,
                       m1.resolution_source AS k_source,
                       m2.resolution_source AS p_source,
                       m1.close_time AS k_close, m2.close_time AS p_close,
                       m1.volume AS k_volume, m2.volume AS p_volume,
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

    @app.get("/pnl")
    def pnl():
        """Paper-trading PnL: realized + unrealized mark-to-market.

        Query ?fmt=json returns the full state dict (used by external
        monitors / scripted alerts).
        """
        from arb_bot.dashboard.pnl import compute_pnl_state
        with db() as conn:
            state = compute_pnl_state(conn)
        if request.args.get("fmt") == "json":
            return jsonify(state)
        return render_template("pnl.html", **state)

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

    @app.get("/dry_run")
    def dry_run():
        """One-shot scan: compute paper signals against current market
        quotes, ranked by fee-adjusted edge. Read-only — does NOT write
        to paper_signals/paper_fills. Use /run_now (POST) to commit.
        """
        cfg = app.config["ARB_CFG"]
        from arb_bot.signal.spread import detect_for_pair

        scan_summary = {
            "total_approved": 0,
            "signals_generated": 0,
            "would_trade": 0,
            "missing_quotes": 0,
            "polarity_unknown": 0,
        }
        reject_reasons: dict[str, int] = {}
        rows: list = []

        with db() as conn:
            approved = conn.execute(
                "SELECT * FROM approved_pairs WHERE active=1"
            ).fetchall()
            scan_summary["total_approved"] = len(approved)

            for p in approved:
                sig = detect_for_pair(conn, cfg, p)
                if sig is None:
                    scan_summary["missing_quotes"] += 1
                    continue
                scan_summary["signals_generated"] += 1
                if sig.polarity == "unknown":
                    scan_summary["polarity_unknown"] += 1
                if sig.would_trade:
                    scan_summary["would_trade"] += 1
                else:
                    reject_reasons[sig.reject_reason or "(unknown)"] = (
                        reject_reasons.get(sig.reject_reason or "(unknown)", 0) + 1
                    )
                # Pull rich market data for display
                kal = conn.execute(
                    "SELECT title, description, resolution_criteria, "
                    "resolution_source, close_time, volume, status, last_seen_ts "
                    "FROM markets WHERE venue='kalshi' AND venue_market_id=?",
                    (p["kalshi_ticker"],),
                ).fetchone()
                poly = conn.execute(
                    "SELECT title, description, resolution_criteria, "
                    "resolution_source, close_time, volume, status, last_seen_ts "
                    "FROM markets WHERE venue='poly_global' AND venue_market_id=?",
                    (p["poly_global_market_id"],),
                ).fetchone()
                now_ts = int(time.time())
                k_last = (kal["last_seen_ts"] if kal else None)
                p_last = (poly["last_seen_ts"] if poly else None)
                rows.append({
                    "pair_id": sig.pair_id,
                    "kalshi_ticker": p["kalshi_ticker"],
                    "poly_global_market_id": p["poly_global_market_id"],
                    "polarity": sig.polarity,
                    "kal_yes": sig.kalshi_yes_mid,
                    "poly_yes": sig.poly_yes_mid,
                    "raw_spread": sig.raw_spread,
                    "edge_bps": sig.fee_adjusted_edge_bps,
                    "direction": sig.direction,
                    "size_units": sig.size_units,
                    "target_capital": sig.target_capital_usd,
                    "would_trade": sig.would_trade,
                    "reject_reason": sig.reject_reason,
                    "k_title": (kal["title"] if kal else "") or "",
                    "p_title": (poly["title"] if poly else "") or "",
                    "k_desc": (kal["description"] if kal else "") or "",
                    "p_desc": (poly["description"] if poly else "") or "",
                    "k_rules": (kal["resolution_criteria"] if kal else "") or "",
                    "p_rules": (poly["resolution_criteria"] if poly else "") or "",
                    "k_source": (kal["resolution_source"] if kal else "") or "",
                    "p_source": (poly["resolution_source"] if poly else "") or "",
                    "k_close": (kal["close_time"] if kal else None),
                    "p_close": (poly["close_time"] if poly else None),
                    "k_volume": (kal["volume"] if kal else 0) or 0,
                    "p_volume": (poly["volume"] if poly else 0) or 0,
                    "k_status": (kal["status"] if kal else "") or "",
                    "p_status": (poly["status"] if poly else "") or "",
                    "k_last_seen": k_last,
                    "p_last_seen": p_last,
                    "k_age_min": (now_ts - k_last) // 60 if k_last else None,
                    "p_age_min": (now_ts - p_last) // 60 if p_last else None,
                    "max_age_min": max(
                        (now_ts - k_last) // 60 if k_last else 0,
                        (now_ts - p_last) // 60 if p_last else 0,
                    ),
                })

        # Top opportunities by fee-adjusted edge — only would-trade signals
        rows_trade = sorted(
            [r for r in rows if r["would_trade"]],
            key=lambda x: -x["edge_bps"],
        )[:50]
        rows_skip = sorted(
            [r for r in rows if not r["would_trade"] and r["raw_spread"] > 0],
            key=lambda x: -x["edge_bps"],
        )[:30]

        # Total estimated profit if we filled every would_trade pair at this size
        total_capital = sum(r["target_capital"] for r in rows_trade)
        total_expected_profit = sum(
            r["target_capital"] * r["edge_bps"] / 10_000.0 for r in rows_trade
        )

        return render_template(
            "dry_run.html",
            scan=scan_summary,
            reject_reasons=sorted(reject_reasons.items(), key=lambda x: -x[1]),
            rows_trade=rows_trade,
            rows_skip=rows_skip,
            total_capital=total_capital,
            total_expected_profit=total_expected_profit,
        )

    @app.post("/refresh_quotes")
    def refresh_quotes():
        """Run a fresh ingest cycle (Kalshi + Polymarket) and redirect back.

        Triggered by the 'Refresh quotes now' button on /dry_run. Takes
        roughly 5-7 minutes; the user gets a flash message confirming
        the run started in the background.
        """
        import subprocess
        cfg = app.config["ARB_CFG"]
        log_path = cfg.data_dir / "logs" / "manual_refresh.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Fire-and-forget: launch the refresh in the background so the
        # HTTP handler returns immediately. The cron's hourly :15 entry
        # does the same thing on a schedule; this just lets the
        # operator trigger it ad-hoc.
        venv_python = Path(__file__).resolve().parents[3] / ".venv" / "bin" / "python"
        script = Path(__file__).resolve().parents[3] / "scripts" / "run_daily_pipeline.py"
        with open(log_path, "ab") as logf:
            subprocess.Popen(
                [str(venv_python), str(script), "--skip-batch"],
                stdout=logf, stderr=subprocess.STDOUT,
                cwd=str(Path(__file__).resolve().parents[3]),
            )
        flash(
            "Quote refresh started in the background (5-7 min). "
            "Re-load /dry_run after a few minutes to see updated prices.",
            "success",
        )
        return redirect(url_for("dry_run"))

    @app.post("/sweep_stale")
    def sweep_stale():
        """Deactivate pairs whose both legs are stale beyond threshold_hours.

        Synchronous; runs the same SQL as scripts/sweep_stale_pairs.py.
        Default threshold 72h matches the CLI default. Triggered from
        the 'Sweep stale pairs' button on /ws_status.
        """
        threshold_hours = float(request.form.get("threshold_hours", "72"))
        threshold_sec = int(threshold_hours * 3600)
        now_ts = int(time.time())
        cutoff = now_ts - threshold_sec
        with db() as conn:
            rows = conn.execute(
                """
                SELECT ap.pair_id
                FROM approved_pairs ap
                LEFT JOIN markets km
                      ON km.venue='kalshi'      AND km.venue_market_id=ap.kalshi_ticker
                LEFT JOIN markets pm
                      ON pm.venue='poly_global' AND pm.venue_market_id=ap.poly_global_market_id
                WHERE ap.active=1
                  AND (km.last_seen_ts IS NULL OR km.last_seen_ts < ?)
                  AND (pm.last_seen_ts IS NULL OR pm.last_seen_ts < ?)
                """,
                (cutoff, cutoff),
            ).fetchall()
            note = (
                f"\n[auto-deactivated by /sweep_stale at {now_ts}: "
                f"both legs stale > {threshold_hours}h]"
            )
            n = 0
            for r in rows:
                conn.execute(
                    "UPDATE approved_pairs SET active=0, "
                    "notes=COALESCE(notes,'') || ? WHERE pair_id=?",
                    (note, r["pair_id"]),
                )
                n += 1
            conn.commit()
        if n:
            flash(f"Deactivated {n} stale pair(s) (>{threshold_hours:g}h on both legs).",
                  "success")
        else:
            flash(f"No pairs above {threshold_hours:g}h staleness.", "warning")
        return redirect(url_for("ws_status"))

    @app.route("/ws_status")
    def ws_status():
        """Health page for the Polymarket WebSocket listener.

        Reads data/ws_listener_status.json (written by the listener
        every 10s) and renders an HTML status board. With ?fmt=json
        returns the raw JSON, useful for cron-driven alerting.
        """
        import json as _json
        import os as _os
        cfg = app.config["ARB_CFG"]
        status_path = cfg.data_dir / "ws_listener_status.json"
        status: dict | None = None
        if status_path.exists():
            try:
                status = _json.loads(status_path.read_text())
            except Exception:
                status = None

        # Process-existence check: even if status file is fresh, if the
        # listener pid is gone the file is just stale.
        listener_alive = False
        if status and status.get("pid"):
            try:
                _os.kill(int(status["pid"]), 0)
                listener_alive = True
            except (OSError, ValueError):
                listener_alive = False

        if request.args.get("fmt") == "json":
            payload = dict(status or {})
            payload["listener_alive"] = listener_alive
            payload["server_time"] = time.time()
            return jsonify(payload)

        # Compute derived fields for the template
        ctx: dict = {"status": status}
        if not status:
            return render_template("ws_status.html", **ctx)

        now = time.time()
        proc_uptime = now - (status.get("process_started_wall_ts") or now)
        sess_uptime = now - (status.get("session_started_wall_ts") or now)
        last_msg_age = (now - status["last_message_wall_ts"]) if status.get("last_message_wall_ts") else None
        msg_per_sec = None
        if sess_uptime > 1 and (status.get("n_book", 0) + status.get("n_price_change", 0)) > 0:
            msg_per_sec = (status.get("n_book", 0) + status.get("n_price_change", 0)) / sess_uptime
        reconnect_rate = (status.get("reconnect_count", 0) / proc_uptime * 3600.0) if proc_uptime > 60 else 0.0

        # Health verdict
        if not listener_alive:
            health_color, health_icon = "var(--bad)", "⛔"
            health_label = "Process not running"
            health_detail = (
                f"Status file says PID {status.get('pid')}, but that process "
                "no longer exists. Restart the listener."
            )
        elif status.get("connection_status") != "connected":
            health_color, health_icon = "var(--warn)", "⚠"
            health_label = f"Connection: {status.get('connection_status', 'unknown')}"
            health_detail = "Listener is alive but not currently connected to the WS endpoint."
        elif last_msg_age is None or last_msg_age > 300:
            health_color, health_icon = "var(--bad)", "⛔"
            health_label = "Connected but silent"
            health_detail = (
                f"No messages received in {int(last_msg_age) if last_msg_age else 'N/A'}s. "
                "Subscription may have failed."
            )
        elif last_msg_age > 30:
            health_color, health_icon = "var(--warn)", "⚠"
            health_label = "Slow message flow"
            health_detail = f"Last message {int(last_msg_age)}s ago — connection may be stalling."
        else:
            health_color, health_icon = "var(--ok)", "✓"
            health_label = "Healthy"
            health_detail = (
                f"{status.get('subscription_count', 0)} tokens subscribed, "
                f"~{(msg_per_sec or 0):.0f} msg/s. Last message {int(last_msg_age)}s ago."
            )

        # Format human-readable durations
        def _fmt_dur(s: float) -> str:
            s = int(s)
            if s < 60:
                return f"{s}s"
            if s < 3600:
                return f"{s//60}m {s%60}s"
            return f"{s//3600}h {(s%3600)//60}m"

        ctx.update({
            "listener_alive": listener_alive,
            "uptime_str": _fmt_dur(proc_uptime),
            "session_uptime_str": _fmt_dur(sess_uptime),
            "last_msg_age_sec": last_msg_age,
            "last_msg_age_str": _fmt_dur(last_msg_age) if last_msg_age else None,
            "msg_per_sec": msg_per_sec,
            "reconnect_rate_per_hr": reconnect_rate,
            "health_color": health_color,
            "health_icon": health_icon,
            "health_label": health_label,
            "health_detail": health_detail,
        })

        # Market freshness histogram + stalest pairs
        with db() as conn:
            buckets_raw = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN ?-last_seen_ts < 30   THEN 1 ELSE 0 END) AS under_30s,
                  SUM(CASE WHEN ?-last_seen_ts BETWEEN 30 AND 119 THEN 1 ELSE 0 END) AS bw_30_2m,
                  SUM(CASE WHEN ?-last_seen_ts BETWEEN 120 AND 599 THEN 1 ELSE 0 END) AS bw_2_10m,
                  SUM(CASE WHEN ?-last_seen_ts BETWEEN 600 AND 3599 THEN 1 ELSE 0 END) AS bw_10_60m,
                  SUM(CASE WHEN ?-last_seen_ts >= 3600 THEN 1 ELSE 0 END) AS over_60m,
                  COUNT(*) AS total
                FROM markets m
                WHERE m.venue='poly_global'
                  AND m.venue_market_id IN (
                    SELECT poly_global_market_id FROM approved_pairs WHERE active=1
                  )
                """,
                (now, now, now, now, now),
            ).fetchone()
            total = buckets_raw["total"] or 1
            buckets = [
                ("< 30 sec",       buckets_raw["under_30s"],  100*buckets_raw["under_30s"]/total,  "var(--ok)"),
                ("30 sec – 2 min", buckets_raw["bw_30_2m"],   100*buckets_raw["bw_30_2m"]/total,   "var(--ok)"),
                ("2 – 10 min",     buckets_raw["bw_2_10m"],   100*buckets_raw["bw_2_10m"]/total,   "var(--warn)"),
                ("10 – 60 min",    buckets_raw["bw_10_60m"],  100*buckets_raw["bw_10_60m"]/total,  "var(--warn)"),
                ("> 60 min",       buckets_raw["over_60m"],   100*buckets_raw["over_60m"]/total,   "var(--bad)"),
            ]
            ctx["freshness"] = {"total": buckets_raw["total"], "buckets": buckets}

            stalest_rows = conn.execute(
                """
                SELECT ap.kalshi_ticker, m.title, m.last_seen_ts
                FROM approved_pairs ap
                JOIN markets m ON m.venue='poly_global' AND m.venue_market_id = ap.poly_global_market_id
                WHERE ap.active=1
                ORDER BY m.last_seen_ts ASC
                LIMIT 10
                """
            ).fetchall()
            stalest = []
            for r in stalest_rows:
                age = now - (r["last_seen_ts"] or now)
                stalest.append({
                    "kalshi_ticker": r["kalshi_ticker"],
                    "title": r["title"],
                    "age_str": _fmt_dur(age),
                })
            ctx["stalest"] = stalest

        return render_template("ws_status.html", **ctx)

    @app.post("/run_now")
    def run_now():
        """Commit a full scan: write paper_signals + paper_fills.

        Mirrors what the cron runloop does on each cycle, but ad-hoc.
        Honors the daily PnL stop + per-pair position cap from
        risk/limits.py. Idempotent — re-running on the same data just
        adds new signal rows; doesn't re-fill prior signals.
        """
        cfg = app.config["ARB_CFG"]
        from arb_bot.executor.paper import simulate_fill
        from arb_bot.risk.limits import check as risk_check
        from arb_bot.signal.spread import detect_for_pair, record_signal

        n_signals = 0
        n_fills = 0
        n_risk_blocks = 0
        with db() as conn:
            approved = conn.execute(
                "SELECT * FROM approved_pairs WHERE active=1"
            ).fetchall()
            for p in approved:
                sig = detect_for_pair(conn, cfg, p)
                if sig is None:
                    continue
                sig_id = record_signal(conn, sig)
                n_signals += 1
                if not sig.would_trade:
                    continue
                ok, reason = risk_check(conn, cfg, sig.pair_id)
                if not ok:
                    n_risk_blocks += 1
                    continue
                simulate_fill(conn, sig_id, sig)
                n_fills += 1
        flash(
            f"Committed: {n_signals} signals scored, {n_fills} paper-filled, "
            f"{n_risk_blocks} risk-blocked.",
            "success",
        )
        return redirect(url_for("dry_run"))

    @app.get("/config")
    def config_page():
        """Live bankroll + risk + venue config so operator can verify
        the bot's working values match expectations before live trading."""
        cfg = app.config["ARB_CFG"]
        # Round-trip friction estimate from signal/spread.py
        from arb_bot.signal.spread import KALSHI_FEE_BPS, POLY_GLOBAL_FEE_BPS
        slippage_bps = 30  # paper executor's SLIPPAGE_BPS
        round_trip_fee = KALSHI_FEE_BPS + POLY_GLOBAL_FEE_BPS
        round_trip_total = round_trip_fee + 2 * slippage_bps  # both legs
        bankroll = cfg.initial_bankroll_usd

        return render_template(
            "config.html",
            cfg=cfg,
            bankroll=bankroll,
            max_position_pct=cfg.paper_max_position_pct,
            target_pct=cfg.paper_per_pair_target_pct,
            min_position_usd=cfg.paper_min_position_usd,
            daily_loss_pct=cfg.paper_daily_max_loss_pct,
            max_position_usd=cfg.paper_max_position_usd,
            target_per_pair_usd=cfg.paper_per_pair_target_usd,
            daily_max_loss_usd=cfg.paper_daily_max_loss_usd,
            min_edge_bps=cfg.paper_min_edge_bps,
            kalshi_fee_bps=KALSHI_FEE_BPS,
            poly_fee_bps=POLY_GLOBAL_FEE_BPS,
            slippage_bps=slippage_bps,
            round_trip_fee=round_trip_fee,
            round_trip_total=round_trip_total,
            max_simultaneous=int(bankroll / max(cfg.paper_per_pair_target_usd, 0.01)),
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
