"""Unit tests for calibration analysis."""
from __future__ import annotations
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


@pytest.fixture(autouse=True)
def _patch_config(monkeypatch):
    import config
    monkeypatch.setattr(config, "TRADES_FILE", "data/trades.json")
    monkeypatch.setattr(config, "PERFORMANCE_FILE", "data/performance.csv")


@pytest.fixture
def analyzer(tmp_path):
    """Create a CalibrationAnalyzer with a temp database."""
    from calibration import CalibrationAnalyzer
    db_path = str(tmp_path / "test.db")
    return CalibrationAnalyzer(db_path=db_path)


@pytest.fixture
def populated_analyzer(analyzer):
    """Analyzer with sample trades for testing."""
    # Insert trades directly into the database
    trades = [
        # Well-calibrated 70% prediction → win
        ("T1", "crypto", "yes", 0.70, 0.55, 0.15, 10, 55.0, 0.10,
         "2026-03-01T10:00:00+00:00", "win", 1.0, 4.50, "2026-03-02T10:00:00+00:00"),
        # Well-calibrated 70% prediction → loss (expected sometimes)
        ("T2", "crypto", "yes", 0.70, 0.55, 0.15, 10, 55.0, 0.10,
         "2026-03-03T10:00:00+00:00", "loss", 0.0, -5.50, "2026-03-04T10:00:00+00:00"),
        # Weather trade — high confidence win
        ("T3", "weather", "yes", 0.85, 0.60, 0.25, 5, 30.0, 0.08,
         "2026-03-05T10:00:00+00:00", "win", 1.0, 2.00, "2026-03-06T10:00:00+00:00"),
        # Economics trade — low confidence loss
        ("T4", "economics", "yes", 0.55, 0.50, 0.05, 3, 15.0, 0.05,
         "2026-03-07T10:00:00+00:00", "loss", 0.0, -1.50, "2026-03-08T10:00:00+00:00"),
        # Crypto NO side win
        ("T5", "crypto", "no", 0.30, 0.55, 0.25, 8, 36.0, 0.10,
         "2026-03-09T10:00:00+00:00", "win", 0.0, 4.40, "2026-03-10T10:00:00+00:00"),
    ]
    for t in trades:
        analyzer.conn.execute("""
            INSERT INTO trades (ticker, category, side, your_prob, market_prob,
                edge_at_entry, num_contracts, cost_usd, kelly_fraction,
                entry_time, outcome, settlement_price, pnl_usd, settlement_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, t)
    analyzer.conn.commit()
    return analyzer


class TestBrierScore:

    def test_no_trades(self, analyzer):
        assert analyzer.brier_score() is None

    def test_perfect_calibration(self, analyzer):
        analyzer.conn.execute("""
            INSERT INTO trades (ticker, category, side, your_prob, market_prob,
                edge_at_entry, num_contracts, cost_usd, kelly_fraction,
                entry_time, outcome, settlement_price, pnl_usd)
            VALUES ('T1', 'crypto', 'yes', 1.0, 0.5, 0.5, 5, 25, 0.1,
                    '2026-03-01T00:00:00+00:00', 'win', 1.0, 2.5)
        """)
        analyzer.conn.commit()
        assert analyzer.brier_score() == 0.0

    def test_worst_calibration(self, analyzer):
        analyzer.conn.execute("""
            INSERT INTO trades (ticker, category, side, your_prob, market_prob,
                edge_at_entry, num_contracts, cost_usd, kelly_fraction,
                entry_time, outcome, settlement_price, pnl_usd)
            VALUES ('T1', 'crypto', 'yes', 1.0, 0.5, 0.5, 5, 25, 0.1,
                    '2026-03-01T00:00:00+00:00', 'loss', 0.0, -2.5)
        """)
        analyzer.conn.commit()
        assert analyzer.brier_score() == 1.0

    def test_with_category_filter(self, populated_analyzer):
        crypto_brier = populated_analyzer.brier_score("crypto")
        weather_brier = populated_analyzer.brier_score("weather")
        assert crypto_brier is not None
        assert weather_brier is not None
        # Weather had a high-confidence win → should have lower Brier
        assert weather_brier < crypto_brier


class TestCalibrationTable:

    def test_empty(self, analyzer):
        assert analyzer.calibration_table() == []

    def test_buckets(self, populated_analyzer):
        cal = populated_analyzer.calibration_table()
        assert len(cal) > 0
        for row in cal:
            assert "bucket" in row
            assert "count" in row
            assert "avg_predicted" in row
            assert "actual_frequency" in row
            assert "gap" in row

    def test_gap_direction(self, populated_analyzer):
        """Gap should be predicted - actual."""
        cal = populated_analyzer.calibration_table()
        for row in cal:
            expected_gap = round(row["avg_predicted"] - row["actual_frequency"], 4)
            assert row["gap"] == expected_gap


class TestConfidenceBias:

    def test_empty(self, analyzer):
        result = analyzer.confidence_bias()
        assert result["direction"] == "insufficient_data"

    def test_has_details(self, populated_analyzer):
        result = populated_analyzer.confidence_bias()
        assert "crypto" in result["details"]
        assert "weather" in result["details"]
        assert result["trade_count"] == 5


class TestEdgeDecay:

    def test_empty(self, analyzer):
        assert analyzer.edge_decay() == []

    def test_monthly_grouping(self, populated_analyzer):
        decay = populated_analyzer.edge_decay()
        assert len(decay) >= 1
        for row in decay:
            assert "avg_edge" in row
            assert "avg_pnl_per_trade" in row
            assert "trade_count" in row


class TestReport:

    def test_full_report_empty(self, analyzer):
        report = analyzer.full_report()
        assert "No settled trades" in report

    def test_full_report_populated(self, populated_analyzer):
        report = populated_analyzer.full_report()
        assert "Brier score" in report
        assert "Calibration Table" in report

    def test_telegram_digest_empty(self, analyzer):
        digest = analyzer.telegram_digest()
        assert "No settled trades" in digest

    def test_telegram_digest_populated(self, populated_analyzer):
        digest = populated_analyzer.telegram_digest()
        assert "Brier" in digest


class TestExport:

    def test_export_csv(self, populated_analyzer, tmp_path):
        filepath = str(tmp_path / "cal.csv")
        result = populated_analyzer.export_csv(filepath)
        assert os.path.exists(result)

        import csv
        with open(result) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 5
        assert "squared_error" in rows[0]
