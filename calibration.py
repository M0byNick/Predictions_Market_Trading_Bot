"""
Calibration analysis for screener probability estimates.

The most important feedback loop in prediction market trading: are your
probability estimates actually calibrated? If you say 70%, does it happen 70%
of the time? This module answers that question with rolling Brier scores,
calibration tables, and edge decay detection.

Usage:
    python calibration.py                 # Full calibration report
    python calibration.py --category crypto   # Single category
    python calibration.py --export        # Export calibration data to CSV
"""
from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

from db import init_db
import config
from log import logger


class CalibrationAnalyzer:
    """Analyzes screener calibration from trade history in SQLite."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.DB_PATH
        self.conn = init_db(self.db_path)

    def _get_settled_trades(self, category: Optional[str] = None,
                            days: Optional[int] = None) -> list:
        """Fetch settled trades, optionally filtered by category and recency."""
        query = "SELECT * FROM trades WHERE outcome IS NOT NULL"
        params = []
        if category:
            query += " AND category = ?"
            params.append(category)
        if days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            query += " AND entry_time >= ?"
            params.append(cutoff)
        query += " ORDER BY entry_time"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ── Brier Score Analysis ────────────────────────────────────────────

    def brier_score(self, category: Optional[str] = None,
                    days: Optional[int] = None) -> Optional[float]:
        """Overall Brier score (lower = better calibration, 0.25 = random)."""
        trades = self._get_settled_trades(category, days)
        if not trades:
            return None
        total = 0.0
        for t in trades:
            actual = 1.0 if t["outcome"] == "win" else 0.0
            predicted = t["your_prob"] if t["side"] == "yes" else (1 - t["your_prob"])
            total += (predicted - actual) ** 2
        return round(total / len(trades), 4)

    def rolling_brier(self, window_days: int = 7,
                      category: Optional[str] = None) -> list:
        """
        Compute Brier score in rolling windows to detect calibration drift.
        Returns list of {window_start, window_end, brier_score, trade_count}.
        """
        trades = self._get_settled_trades(category)
        if not trades:
            return []

        results = []
        # Group trades by week
        buckets = defaultdict(list)
        for t in trades:
            ts = t.get("settlement_time") or t["entry_time"]
            try:
                dt = datetime.fromisoformat(ts)
            except ValueError:
                continue
            # Bucket by start of window_days period
            period_start = dt - timedelta(days=dt.timetuple().tm_yday % window_days)
            bucket_key = period_start.strftime("%Y-%m-%d")
            buckets[bucket_key].append(t)

        for bucket_key in sorted(buckets.keys()):
            bucket_trades = buckets[bucket_key]
            brier = 0.0
            for t in bucket_trades:
                actual = 1.0 if t["outcome"] == "win" else 0.0
                predicted = t["your_prob"] if t["side"] == "yes" else (1 - t["your_prob"])
                brier += (predicted - actual) ** 2
            brier /= len(bucket_trades)
            results.append({
                "window_start": bucket_key,
                "brier_score": round(brier, 4),
                "trade_count": len(bucket_trades),
            })

        return results

    # ── Calibration Table ───────────────────────────────────────────────

    def calibration_table(self, category: Optional[str] = None,
                          days: Optional[int] = None,
                          n_buckets: int = 10) -> list:
        """
        Group trades by predicted probability decile and compare to actual
        win rate. A perfectly calibrated model has predicted == actual in
        every bucket.

        Returns list of {bucket, count, avg_predicted, actual_frequency, gap}.
        """
        trades = self._get_settled_trades(category, days)
        if not trades:
            return []

        buckets = defaultdict(lambda: {"predictions": [], "outcomes": []})
        for t in trades:
            predicted = t["your_prob"] if t["side"] == "yes" else (1 - t["your_prob"])
            actual = 1.0 if t["outcome"] == "win" else 0.0

            # Round to nearest bucket (e.g., 0.67 -> "60-70%")
            bucket_idx = min(int(predicted * n_buckets), n_buckets - 1)
            lo = bucket_idx * (100 // n_buckets)
            hi = lo + (100 // n_buckets)
            bucket_key = f"{lo}-{hi}%"

            buckets[bucket_key]["predictions"].append(predicted)
            buckets[bucket_key]["outcomes"].append(actual)

        results = []
        for bucket_key in sorted(buckets.keys(), key=lambda x: int(x.split("-")[0])):
            data = buckets[bucket_key]
            avg_pred = sum(data["predictions"]) / len(data["predictions"])
            avg_outcome = sum(data["outcomes"]) / len(data["outcomes"])
            results.append({
                "bucket": bucket_key,
                "count": len(data["predictions"]),
                "avg_predicted": round(avg_pred, 4),
                "actual_frequency": round(avg_outcome, 4),
                "gap": round(avg_pred - avg_outcome, 4),
            })

        return results

    # ── Edge Decay Detection ────────────────────────────────────────────

    def edge_decay(self, window_days: int = 30,
                   category: Optional[str] = None) -> list:
        """
        Track average realized edge over rolling windows to detect if
        your edge is shrinking (markets adapting, model going stale).

        Returns list of {window_start, avg_edge, avg_pnl_per_trade, trade_count}.
        """
        trades = self._get_settled_trades(category)
        if not trades:
            return []

        buckets = defaultdict(list)
        for t in trades:
            ts = t.get("settlement_time") or t["entry_time"]
            try:
                dt = datetime.fromisoformat(ts)
            except ValueError:
                continue
            month_key = dt.strftime("%Y-%m")
            buckets[month_key].append(t)

        results = []
        for month in sorted(buckets.keys()):
            month_trades = buckets[month]
            avg_edge = sum(t["edge_at_entry"] for t in month_trades) / len(month_trades)
            avg_pnl = sum(t.get("pnl_usd", 0) or 0 for t in month_trades) / len(month_trades)
            results.append({
                "window_start": month,
                "avg_edge": round(avg_edge, 4),
                "avg_pnl_per_trade": round(avg_pnl, 2),
                "trade_count": len(month_trades),
            })

        return results

    # ── Overconfidence / Underconfidence Detection ──────────────────────

    def confidence_bias(self, category: Optional[str] = None) -> dict:
        """
        Detect systematic overconfidence or underconfidence.

        Returns {bias, direction, details} where:
          - bias: average (predicted - actual) across all trades
          - direction: 'overconfident' if bias > 0, 'underconfident' if < 0
          - details: per-category breakdown
        """
        trades = self._get_settled_trades(category)
        if not trades:
            return {"bias": 0.0, "direction": "insufficient_data", "details": {}}

        gaps = []
        cat_gaps = defaultdict(list)
        for t in trades:
            predicted = t["your_prob"] if t["side"] == "yes" else (1 - t["your_prob"])
            actual = 1.0 if t["outcome"] == "win" else 0.0
            gap = predicted - actual
            gaps.append(gap)
            cat_gaps[t["category"]].append(gap)

        avg_bias = sum(gaps) / len(gaps)
        direction = "overconfident" if avg_bias > 0.02 else (
            "underconfident" if avg_bias < -0.02 else "well_calibrated"
        )

        details = {}
        for cat, cat_gap_list in cat_gaps.items():
            cat_bias = sum(cat_gap_list) / len(cat_gap_list)
            details[cat] = {
                "bias": round(cat_bias, 4),
                "trade_count": len(cat_gap_list),
            }

        return {
            "bias": round(avg_bias, 4),
            "direction": direction,
            "trade_count": len(trades),
            "details": details,
        }

    # ── Phase 6: Advanced Diagnostics ──────────────────────────────────

    def expected_vs_realized_edge(self, category: Optional[str] = None) -> list:
        """
        Compare claimed edge at entry to actual win rate per edge bucket.
        If claimed edge is 20% but realized win rate is 5%, model is drifting.
        """
        trades = self._get_settled_trades(category)
        if not trades:
            return []

        buckets = defaultdict(lambda: {"edges": [], "outcomes": []})
        for t in trades:
            edge = t["edge_at_entry"]
            actual = 1.0 if t["outcome"] == "win" else 0.0
            # Bucket by 10% edge ranges
            bucket_idx = min(int(edge * 10), 4)  # 0-10%, 10-20%, ..., 40%+
            lo = bucket_idx * 10
            hi = lo + 10 if bucket_idx < 4 else 50
            key = f"{lo}-{hi}%"
            buckets[key]["edges"].append(edge)
            buckets[key]["outcomes"].append(actual)

        results = []
        for key in sorted(buckets.keys(), key=lambda x: int(x.split("-")[0])):
            data = buckets[key]
            avg_edge = sum(data["edges"]) / len(data["edges"])
            win_rate = sum(data["outcomes"]) / len(data["outcomes"])
            results.append({
                "edge_bucket": key,
                "count": len(data["edges"]),
                "avg_claimed_edge": round(avg_edge, 4),
                "actual_win_rate": round(win_rate, 4),
                "gap": round(avg_edge - win_rate, 4),
            })
        return results

    def brier_by_days_out(self, category: Optional[str] = None) -> list:
        """
        Brier score by days-to-settlement to detect where models perform
        best/worst. Weather should improve near settlement; econ should
        degrade far out.
        """
        trades = self._get_settled_trades(category)
        if not trades:
            return []

        buckets = defaultdict(list)
        for t in trades:
            entry = t.get("entry_time", "")
            settle = t.get("settlement_time", "")
            if not entry or not settle:
                continue
            try:
                entry_dt = datetime.fromisoformat(entry)
                settle_dt = datetime.fromisoformat(settle)
                days = max((settle_dt - entry_dt).days, 0)
            except Exception:
                continue

            predicted = t["your_prob"] if t["side"] == "yes" else (1 - t["your_prob"])
            actual = 1.0 if t["outcome"] == "win" else 0.0
            sq_err = (predicted - actual) ** 2

            # Bucket: 0d, 1d, 2-3d, 4-7d, 8-14d, 15+d
            if days == 0:
                key = "0d"
            elif days == 1:
                key = "1d"
            elif days <= 3:
                key = "2-3d"
            elif days <= 7:
                key = "4-7d"
            elif days <= 14:
                key = "8-14d"
            else:
                key = "15+d"
            buckets[key].append({"sq_err": sq_err, "cat": t["category"]})

        order = ["0d", "1d", "2-3d", "4-7d", "8-14d", "15+d"]
        results = []
        for key in order:
            if key not in buckets:
                continue
            items = buckets[key]
            brier = sum(i["sq_err"] for i in items) / len(items)
            results.append({
                "days_bucket": key,
                "count": len(items),
                "brier": round(brier, 4),
            })
        return results

    def penny_market_split(self, threshold: float = 0.05) -> dict:
        """
        Separate calibration for penny markets (≤5¢) vs normal markets.
        Penny markets are where most weather losses occurred pre-fix.
        """
        trades = self._get_settled_trades()
        if not trades:
            return {}

        result = {}
        for label, filter_fn in [
            (f"penny (≤{int(threshold*100)}¢)", lambda t: t["market_prob"] <= threshold),
            (f"normal (>{int(threshold*100)}¢)", lambda t: t["market_prob"] > threshold),
        ]:
            subset = [t for t in trades if filter_fn(t)]
            if not subset:
                result[label] = {"count": 0}
                continue

            wins = sum(1 for t in subset if t["outcome"] == "win")
            brier = sum(
                ((t["your_prob"] if t["side"] == "yes" else 1-t["your_prob"])
                 - (1.0 if t["outcome"] == "win" else 0.0)) ** 2
                for t in subset
            ) / len(subset)
            total_pnl = sum(t.get("pnl_usd", 0) or 0 for t in subset)

            result[label] = {
                "count": len(subset),
                "wins": wins,
                "hit_rate": round(wins / len(subset), 4),
                "brier": round(brier, 4),
                "pnl": round(total_pnl, 2),
            }
        return result

    def model_version_comparison(self) -> dict:
        """
        Compare calibration before and after the contract-type fix.
        Uses entry_time as a proxy for code version.
        """
        # The fix went live around 2026-03-25T04:00:00Z
        cutoff = "2026-03-25T04:00:00"
        result = {}

        for label, where in [
            ("pre-fix", f"entry_time < '{cutoff}'"),
            ("post-fix", f"entry_time >= '{cutoff}'"),
        ]:
            rows = self.conn.execute(
                f"SELECT * FROM trades WHERE outcome IS NOT NULL AND {where}"
            ).fetchall()
            trades = [dict(r) for r in rows]
            if not trades:
                result[label] = {"count": 0}
                continue

            wins = sum(1 for t in trades if t["outcome"] == "win")
            brier = sum(
                ((t["your_prob"] if t["side"] == "yes" else 1-t["your_prob"])
                 - (1.0 if t["outcome"] == "win" else 0.0)) ** 2
                for t in trades
            ) / len(trades)
            bias = sum(
                (t["your_prob"] if t["side"] == "yes" else 1-t["your_prob"])
                - (1.0 if t["outcome"] == "win" else 0.0)
                for t in trades
            ) / len(trades)
            total_pnl = sum(t.get("pnl_usd", 0) or 0 for t in trades)

            result[label] = {
                "count": len(trades),
                "wins": wins,
                "hit_rate": round(wins / len(trades), 4),
                "brier": round(brier, 4),
                "bias": round(bias, 4),
                "pnl": round(total_pnl, 2),
            }
        return result

    # ── Reports ─────────────────────────────────────────────────────────

    def full_report(self, category: Optional[str] = None) -> str:
        """Generate a comprehensive calibration report."""
        lines = []
        label = category.upper() if category else "ALL CATEGORIES"
        lines.append(f"\n{'═' * 60}")
        lines.append(f"  CALIBRATION REPORT: {label}")
        lines.append(f"{'═' * 60}")

        # Overall Brier
        brier = self.brier_score(category)
        if brier is not None:
            lines.append(f"\n  Brier score: {brier:.4f} (0.0 perfect, 0.25 random)")
        else:
            lines.append("\n  No settled trades to analyze.")
            return "\n".join(lines)

        # Confidence bias
        bias = self.confidence_bias(category)
        lines.append(f"  Calibration bias: {bias['bias']:+.4f} ({bias['direction']})")
        lines.append(f"  Trades analyzed: {bias['trade_count']}")

        if bias["details"]:
            lines.append(f"\n  {'─' * 50}")
            lines.append(f"  Per-Category Bias")
            lines.append(f"  {'─' * 50}")
            for cat, detail in sorted(bias["details"].items()):
                lines.append(f"  {cat:>12s}: {detail['bias']:+.4f} ({detail['trade_count']} trades)")

        # Calibration table
        cal = self.calibration_table(category)
        if cal:
            lines.append(f"\n  {'─' * 50}")
            lines.append(f"  Calibration Table (predicted vs actual)")
            lines.append(f"  {'─' * 50}")
            lines.append(f"  {'Bucket':>10s} {'Count':>6s} {'Predicted':>10s} {'Actual':>10s} {'Gap':>8s}")
            for row in cal:
                lines.append(
                    f"  {row['bucket']:>10s} {row['count']:>6d} "
                    f"{row['avg_predicted']:>10.1%} {row['actual_frequency']:>10.1%} "
                    f"{row['gap']:>+8.1%}"
                )

        # Edge decay
        decay = self.edge_decay(category=category)
        if decay:
            lines.append(f"\n  {'─' * 50}")
            lines.append(f"  Edge Trend (monthly)")
            lines.append(f"  {'─' * 50}")
            lines.append(f"  {'Month':>10s} {'Trades':>7s} {'Avg Edge':>10s} {'Avg P&L':>10s}")
            for row in decay:
                lines.append(
                    f"  {row['window_start']:>10s} {row['trade_count']:>7d} "
                    f"{row['avg_edge']:>10.1%} ${row['avg_pnl_per_trade']:>9.2f}"
                )

        # Rolling Brier
        rolling = self.rolling_brier(category=category)
        if rolling and len(rolling) > 1:
            lines.append(f"\n  {'─' * 50}")
            lines.append(f"  Rolling Brier Score (weekly)")
            lines.append(f"  {'─' * 50}")
            for row in rolling:
                bar = "█" * int(row["brier_score"] * 40)
                lines.append(
                    f"  {row['window_start']:>10s} {row['brier_score']:.4f} "
                    f"({row['trade_count']:>3d} trades) {bar}"
                )

        # Expected vs Realized edge
        evr = self.expected_vs_realized_edge(category)
        if evr:
            lines.append(f"\n  {'─' * 50}")
            lines.append(f"  Expected vs Realized Edge")
            lines.append(f"  {'─' * 50}")
            lines.append(f"  {'Edge Bucket':>12s} {'Count':>6s} {'Claimed':>8s} {'Actual':>8s} {'Gap':>8s}")
            for row in evr:
                lines.append(
                    f"  {row['edge_bucket']:>12s} {row['count']:>6d} "
                    f"{row['avg_claimed_edge']:>8.1%} {row['actual_win_rate']:>8.1%} "
                    f"{row['gap']:>+8.1%}"
                )

        # Brier by days to settlement
        bdays = self.brier_by_days_out(category)
        if bdays:
            lines.append(f"\n  {'─' * 50}")
            lines.append(f"  Brier Score by Days to Settlement")
            lines.append(f"  {'─' * 50}")
            lines.append(f"  {'Days':>8s} {'Count':>6s} {'Brier':>8s}")
            for row in bdays:
                bar = "█" * int(row["brier"] * 40)
                lines.append(f"  {row['days_bucket']:>8s} {row['count']:>6d} {row['brier']:>8.4f} {bar}")

        # Penny market split
        penny = self.penny_market_split()
        if penny:
            lines.append(f"\n  {'─' * 50}")
            lines.append(f"  Penny Market Split (≤5¢ vs >5¢)")
            lines.append(f"  {'─' * 50}")
            for label, data in penny.items():
                if data["count"] == 0:
                    continue
                lines.append(
                    f"  {label:>16s}: {data['count']:>5d} trades, "
                    f"{data['hit_rate']:>5.1%} hit, "
                    f"Brier={data['brier']:.4f}, "
                    f"P&L=${data['pnl']:>10,.2f}"
                )

        # Model version comparison
        mvc = self.model_version_comparison()
        if mvc:
            lines.append(f"\n  {'─' * 50}")
            lines.append(f"  Model Version Comparison (pre-fix vs post-fix)")
            lines.append(f"  {'─' * 50}")
            for label, data in mvc.items():
                if data["count"] == 0:
                    lines.append(f"  {label:>10s}: no settled trades")
                    continue
                lines.append(
                    f"  {label:>10s}: {data['count']:>5d} trades, "
                    f"{data['hit_rate']:>5.1%} hit, "
                    f"Brier={data['brier']:.4f}, "
                    f"bias={data['bias']:+.4f}, "
                    f"P&L=${data['pnl']:>10,.2f}"
                )

        lines.append(f"\n{'═' * 60}\n")
        return "\n".join(lines)

    def export_csv(self, filepath: str = "data/calibration.csv",
                   category: Optional[str] = None) -> str:
        """Export calibration data to CSV."""
        trades = self._get_settled_trades(category)
        if not trades:
            logger.info("No settled trades to export")
            return filepath

        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        fieldnames = [
            "id", "ticker", "category", "side", "your_prob", "market_prob",
            "edge_at_entry", "outcome", "pnl_usd", "predicted_for_side",
            "actual_for_side", "squared_error", "entry_time", "settlement_time",
        ]
        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for t in trades:
                predicted = t["your_prob"] if t["side"] == "yes" else (1 - t["your_prob"])
                actual = 1.0 if t["outcome"] == "win" else 0.0
                writer.writerow({
                    "id": t["id"],
                    "ticker": t["ticker"],
                    "category": t["category"],
                    "side": t["side"],
                    "your_prob": t["your_prob"],
                    "market_prob": t["market_prob"],
                    "edge_at_entry": t["edge_at_entry"],
                    "outcome": t["outcome"],
                    "pnl_usd": t.get("pnl_usd"),
                    "predicted_for_side": round(predicted, 4),
                    "actual_for_side": actual,
                    "squared_error": round((predicted - actual) ** 2, 4),
                    "entry_time": t["entry_time"],
                    "settlement_time": t.get("settlement_time"),
                })

        logger.info("Calibration data exported to %s (%d trades)", filepath, len(trades))
        return filepath

    def telegram_digest(self, category: Optional[str] = None) -> str:
        """Generate a compact calibration summary for Telegram."""
        brier = self.brier_score(category)
        bias = self.confidence_bias(category)
        label = category.upper() if category else "ALL"

        if brier is None:
            return f"📊 <b>Calibration ({label})</b>\nNo settled trades yet."

        emoji = "🟢" if brier < 0.20 else ("🟡" if brier < 0.25 else "🔴")
        lines = [
            f"📊 <b>Calibration ({label})</b>",
            f"{emoji} Brier: {brier:.4f}",
            f"Bias: {bias['bias']:+.4f} ({bias['direction']})",
            f"Trades: {bias['trade_count']}",
        ]

        # Per-category one-liner
        for cat, detail in sorted(bias.get("details", {}).items()):
            lines.append(f"  {cat}: {detail['bias']:+.3f} ({detail['trade_count']})")

        return "\n".join(lines)


if __name__ == "__main__":
    category = None
    export = False

    args = sys.argv[1:]
    if "--category" in args:
        idx = args.index("--category")
        if idx + 1 < len(args):
            category = args[idx + 1]
    if "--export" in args:
        export = True

    analyzer = CalibrationAnalyzer()

    if export:
        path = analyzer.export_csv(category=category)
        print(f"Exported to {path}")
    else:
        print(analyzer.full_report(category))
