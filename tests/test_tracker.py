"""Unit tests for the trade tracker."""
from __future__ import annotations
import sys
import os
import json
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


@pytest.fixture(autouse=True)
def _patch_config(monkeypatch):
    import config
    monkeypatch.setattr(config, "TRADES_FILE", "data/trades.json")
    monkeypatch.setattr(config, "PERFORMANCE_FILE", "data/performance.csv")


@pytest.fixture
def tracker(tmp_path):
    """Create a Tracker with temp files."""
    from tracker import Tracker
    trades_file = str(tmp_path / "trades.json")
    perf_file = str(tmp_path / "performance.csv")
    pending_file = str(tmp_path / "pending.json")
    return Tracker(trades_file=trades_file, performance_file=perf_file,
                   pending_file=pending_file)


class TestLogTrade:

    def test_basic_log(self, tracker):
        trade = tracker.log_trade(
            ticker="KXBTC-T100000", category="crypto", side="yes",
            your_prob=0.70, market_prob=0.55,
            num_contracts=10, cost_usd=55.0,
            kelly_fraction=0.10,
        )
        assert trade["id"] == 1
        assert trade["ticker"] == "KXBTC-T100000"
        assert trade["outcome"] is None
        assert len(tracker.trades) == 1

    def test_persistence(self, tracker):
        tracker.log_trade("T1", "crypto", "yes", 0.7, 0.5, 5, 25.0, 0.1)
        # Reload from disk
        from tracker import Tracker
        t2 = Tracker(trades_file=tracker.trades_file,
                     performance_file=tracker.performance_file,
                     pending_file=tracker.pending_file)
        assert len(t2.trades) == 1
        assert t2.trades[0]["ticker"] == "T1"

    def test_incremental_ids(self, tracker):
        tracker.log_trade("T1", "crypto", "yes", 0.7, 0.5, 5, 25.0, 0.1)
        tracker.log_trade("T2", "weather", "no", 0.3, 0.6, 3, 18.0, 0.08)
        assert tracker.trades[0]["id"] == 1
        assert tracker.trades[1]["id"] == 2

    def test_edge_at_entry_yes(self, tracker):
        trade = tracker.log_trade("T1", "crypto", "yes", 0.70, 0.55, 5, 27.5, 0.1)
        assert trade["edge_at_entry"] == round(0.70 - 0.55, 4)

    def test_edge_at_entry_no(self, tracker):
        trade = tracker.log_trade("T1", "crypto", "no", 0.30, 0.55, 5, 22.5, 0.1)
        # For NO side: (1 - your_prob) - (1 - market_prob) = market_prob - your_prob
        expected = round((1 - 0.30) - (1 - 0.55), 4)
        assert trade["edge_at_entry"] == expected


class TestRecordOutcome:

    def test_win(self, tracker):
        tracker.log_trade("T1", "crypto", "yes", 0.70, 0.55, 10, 55.0, 0.1)
        tracker.record_outcome(1, "win", 1.0)
        t = tracker.trades[0]
        assert t["outcome"] == "win"
        # Bought YES at 0.55, won → profit = (1.0 - 0.55) * 10 = $4.50
        assert t["pnl_usd"] == 4.50

    def test_loss(self, tracker):
        tracker.log_trade("T1", "crypto", "yes", 0.70, 0.55, 10, 55.0, 0.1)
        tracker.record_outcome(1, "loss", 0.0)
        t = tracker.trades[0]
        assert t["outcome"] == "loss"
        # Bought YES at 0.55, lost → loss = -0.55 * 10 = -$5.50
        assert t["pnl_usd"] == -5.50

    def test_no_side_win(self, tracker):
        tracker.log_trade("T1", "crypto", "no", 0.30, 0.55, 10, 45.0, 0.1)
        tracker.record_outcome(1, "win", 0.0)
        t = tracker.trades[0]
        # Bought NO at cost_per = 1 - 0.55 = 0.45, won → (1.0 - 0.45) * 10 = $5.50
        assert t["pnl_usd"] == 5.50


class TestMetrics:

    def test_hit_rate_empty(self, tracker):
        assert tracker.hit_rate() == 0.0

    def test_hit_rate(self, tracker):
        tracker.log_trade("T1", "crypto", "yes", 0.7, 0.5, 5, 25.0, 0.1)
        tracker.log_trade("T2", "crypto", "yes", 0.7, 0.5, 5, 25.0, 0.1)
        tracker.record_outcome(1, "win", 1.0)
        tracker.record_outcome(2, "loss", 0.0)
        assert tracker.hit_rate() == 0.5

    def test_brier_score_perfect(self, tracker):
        tracker.log_trade("T1", "crypto", "yes", 1.0, 0.5, 5, 25.0, 0.1)
        tracker.record_outcome(1, "win", 1.0)
        # Predicted 1.0, actual 1.0 → (1.0 - 1.0)^2 = 0.0
        assert tracker.brier_score() == 0.0

    def test_brier_score_worst(self, tracker):
        tracker.log_trade("T1", "crypto", "yes", 1.0, 0.5, 5, 25.0, 0.1)
        tracker.record_outcome(1, "loss", 0.0)
        # Predicted 1.0, actual 0.0 → (1.0 - 0.0)^2 = 1.0
        assert tracker.brier_score() == 1.0

    def test_brier_score_none_when_empty(self, tracker):
        assert tracker.brier_score() is None

    def test_total_pnl(self, tracker):
        tracker.log_trade("T1", "crypto", "yes", 0.7, 0.5, 10, 50.0, 0.1)
        tracker.log_trade("T2", "crypto", "yes", 0.7, 0.5, 10, 50.0, 0.1)
        tracker.record_outcome(1, "win", 1.0)
        tracker.record_outcome(2, "loss", 0.0)
        # Win: (1-0.5)*10 = $5.00, Loss: -0.5*10 = -$5.00
        assert tracker.total_pnl() == 0.0

    def test_category_filter(self, tracker):
        tracker.log_trade("T1", "crypto", "yes", 0.7, 0.5, 10, 50.0, 0.1)
        tracker.log_trade("T2", "weather", "yes", 0.7, 0.5, 10, 50.0, 0.1)
        tracker.record_outcome(1, "win", 1.0)
        tracker.record_outcome(2, "loss", 0.0)
        assert tracker.hit_rate("crypto") == 1.0
        assert tracker.hit_rate("weather") == 0.0

    def test_bankroll_curve(self, tracker):
        tracker.log_trade("T1", "crypto", "yes", 0.7, 0.5, 10, 50.0, 0.1)
        tracker.record_outcome(1, "win", 1.0)
        curve = tracker.bankroll_curve()
        assert len(curve) == 1
        assert curve[0]["cumulative_pnl"] == 5.0


class TestPendingOrders:

    def test_mark_and_get(self, tracker):
        tracker.mark_pending("T1", "yes", 10, 55.0, "order-123")
        pending = tracker.get_pending_orders()
        assert len(pending) == 1
        assert pending[0]["order_id"] == "order-123"

    def test_clear_pending(self, tracker):
        tracker.mark_pending("T1", "yes", 10, 55.0, "order-123")
        tracker.clear_pending("T1", "yes")
        assert len(tracker.get_pending_orders()) == 0

    def test_clear_only_matching(self, tracker):
        tracker.mark_pending("T1", "yes", 10, 55.0, "order-1")
        tracker.mark_pending("T2", "no", 5, 22.5, "order-2")
        tracker.clear_pending("T1", "yes")
        pending = tracker.get_pending_orders()
        assert len(pending) == 1
        assert pending[0]["ticker"] == "T2"

    def test_persistence(self, tracker):
        tracker.mark_pending("T1", "yes", 10, 55.0, "order-123")
        from tracker import Tracker
        t2 = Tracker(trades_file=tracker.trades_file,
                     performance_file=tracker.performance_file,
                     pending_file=tracker.pending_file)
        assert len(t2.get_pending_orders()) == 1
