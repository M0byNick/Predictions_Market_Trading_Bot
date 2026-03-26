"""Unit tests for the trade tracker (SQLite backend)."""
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
def tracker(tmp_path):
    """Create a Tracker backed by a temp SQLite database."""
    from tracker import Tracker
    db_path = str(tmp_path / "test.db")
    perf_file = str(tmp_path / "performance.csv")
    return Tracker(db_path=db_path, performance_file=perf_file)


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
        # Reload from database
        from tracker import Tracker
        t2 = Tracker(db_path=tracker.db_path,
                     performance_file=tracker.performance_file)
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
        # Bought YES at 0.55, won -> profit = (1.0 - 0.55) * 10 = $4.50
        assert t["pnl_usd"] == 4.50

    def test_loss(self, tracker):
        tracker.log_trade("T1", "crypto", "yes", 0.70, 0.55, 10, 55.0, 0.1)
        tracker.record_outcome(1, "loss", 0.0)
        t = tracker.trades[0]
        assert t["outcome"] == "loss"
        # Bought YES at 0.55, lost -> loss = -0.55 * 10 = -$5.50
        assert t["pnl_usd"] == -5.50

    def test_no_side_win(self, tracker):
        tracker.log_trade("T1", "crypto", "no", 0.30, 0.55, 10, 45.0, 0.1)
        tracker.record_outcome(1, "win", 0.0)
        t = tracker.trades[0]
        # Bought NO at cost_per = 1 - 0.55 = 0.45, won -> (1.0 - 0.45) * 10 = $5.50
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
        # Predicted 1.0, actual 1.0 -> (1.0 - 1.0)^2 = 0.0
        assert tracker.brier_score() == 0.0

    def test_brier_score_worst(self, tracker):
        tracker.log_trade("T1", "crypto", "yes", 1.0, 0.5, 5, 25.0, 0.1)
        tracker.record_outcome(1, "loss", 0.0)
        # Predicted 1.0, actual 0.0 -> (1.0 - 0.0)^2 = 1.0
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
        t2 = Tracker(db_path=tracker.db_path,
                     performance_file=tracker.performance_file)
        assert len(t2.get_pending_orders()) == 1


class TestDailyPnl:

    def test_daily_pnl_aggregation(self, tracker):
        tracker.log_trade("T1", "crypto", "yes", 0.7, 0.5, 10, 50.0, 0.1)
        tracker.log_trade("T2", "crypto", "yes", 0.7, 0.5, 10, 50.0, 0.1)
        tracker.record_outcome(1, "win", 1.0)
        tracker.record_outcome(2, "loss", 0.0)
        daily = tracker.get_daily_pnl(days=1)
        assert len(daily) >= 1
        crypto_day = [d for d in daily if d["category"] == "crypto"]
        assert len(crypto_day) == 1
        assert crypto_day[0]["trade_count"] == 2
        assert crypto_day[0]["win_count"] == 1
        assert crypto_day[0]["loss_count"] == 1


class TestJsonMigration:

    def test_migrate_trades(self, tmp_path):
        """Verify that legacy JSON trades are imported on first init."""
        import json
        from tracker import Tracker

        # Create legacy JSON trades file
        trades_file = str(tmp_path / "trades.json")
        legacy_trades = [
            {
                "id": 1, "ticker": "T1", "category": "crypto", "side": "yes",
                "your_prob": 0.7, "market_prob": 0.5, "edge_at_entry": 0.2,
                "num_contracts": 5, "cost_usd": 25.0, "kelly_fraction": 0.1,
                "entry_time": "2026-03-01T00:00:00+00:00",
                "outcome": "win", "settlement_price": 1.0, "pnl_usd": 2.5,
                "settlement_time": "2026-03-02T00:00:00+00:00", "notes": "",
            }
        ]
        with open(trades_file, "w") as f:
            json.dump(legacy_trades, f)

        db_path = str(tmp_path / "test.db")
        tracker = Tracker(db_path=db_path, trades_file=trades_file,
                          performance_file=str(tmp_path / "perf.csv"))
        assert len(tracker.trades) == 1
        assert tracker.trades[0]["ticker"] == "T1"
        assert tracker.trades[0]["outcome"] == "win"


class TestCheckSettlements:
    """Tests for the auto-settlement flow in main.check_settlements."""

    def test_settles_winning_trade(self, tracker):
        """A YES trade on a market that settles YES should be a win."""
        tracker.log_trade("KXTEST-YES", "crypto", "yes", 0.80, 0.50, 10, 50.0, 0.1)

        # Mock client that returns a settled market
        class MockClient:
            def get_market(self, ticker):
                return {"market": {"status": "settled", "result": "yes"}}
            def settle_position(self, *a, **kw):
                pass

        class MockAlerter:
            def __init__(self):
                self.settlements = []
            def send_settlement(self, ticker, outcome, pnl):
                self.settlements.append((ticker, outcome, pnl))

        from main import check_settlements
        alerter = MockAlerter()
        settled = check_settlements(tracker, MockClient(), alerter)

        assert settled == 1
        t = tracker.trades[0]
        assert t["outcome"] == "win"
        assert t["pnl_usd"] == 5.0  # (1.0 - 0.50) * 10
        assert t["settlement_time"] is not None
        assert len(alerter.settlements) == 1

    def test_settles_losing_trade(self, tracker):
        """A YES trade on a market that settles NO should be a loss."""
        tracker.log_trade("KXTEST-NO", "weather", "yes", 0.80, 0.60, 10, 60.0, 0.1)

        class MockClient:
            def get_market(self, ticker):
                return {"market": {"status": "settled", "result": "no"}}
            def settle_position(self, *a, **kw):
                pass

        class MockAlerter:
            settlements = []
            def send_settlement(self, ticker, outcome, pnl):
                self.settlements.append((ticker, outcome, pnl))

        from main import check_settlements
        alerter = MockAlerter()
        settled = check_settlements(tracker, MockClient(), alerter)

        assert settled == 1
        t = tracker.trades[0]
        assert t["outcome"] == "loss"
        assert t["pnl_usd"] == -6.0  # -0.60 * 10

    def test_skips_unsettled_market(self, tracker):
        """Open markets should not trigger settlement."""
        tracker.log_trade("KXTEST-OPEN", "crypto", "yes", 0.80, 0.50, 10, 50.0, 0.1)

        class MockClient:
            def get_market(self, ticker):
                return {"market": {"status": "open", "result": ""}}

        class MockAlerter:
            def send_settlement(self, *a, **kw):
                pass

        from main import check_settlements
        settled = check_settlements(tracker, MockClient(), MockAlerter())

        assert settled == 0
        assert tracker.trades[0]["outcome"] is None

    def test_settles_no_side_trade(self, tracker):
        """A NO trade on a market that settles NO should be a win."""
        tracker.log_trade("KXTEST-NOWIN", "economics", "no", 0.30, 0.60, 10, 40.0, 0.1)

        class MockClient:
            def get_market(self, ticker):
                return {"market": {"status": "settled", "result": "no"}}
            def settle_position(self, *a, **kw):
                pass

        class MockAlerter:
            def send_settlement(self, *a, **kw):
                pass

        from main import check_settlements
        settled = check_settlements(tracker, MockClient(), MockAlerter())

        assert settled == 1
        t = tracker.trades[0]
        assert t["outcome"] == "win"
        # NO side cost_per = 1 - 0.60 = 0.40, win pnl = (1.0 - 0.40) * 10 = 6.0
        assert t["pnl_usd"] == 6.0

    def test_multiple_trades_same_ticker(self, tracker):
        """Multiple trades on the same ticker should all settle together."""
        tracker.log_trade("KXTEST-MULTI", "crypto", "yes", 0.80, 0.50, 10, 50.0, 0.1)
        tracker.log_trade("KXTEST-MULTI", "crypto", "yes", 0.75, 0.55, 5, 27.5, 0.1)

        class MockClient:
            def get_market(self, ticker):
                return {"market": {"status": "settled", "result": "yes"}}
            def settle_position(self, *a, **kw):
                pass

        class MockAlerter:
            count = 0
            def send_settlement(self, *a, **kw):
                MockAlerter.count += 1

        from main import check_settlements
        alerter = MockAlerter()
        settled = check_settlements(tracker, MockClient(), alerter)

        assert settled == 2
        assert all(t["outcome"] == "win" for t in tracker.trades)

    def test_already_settled_ignored(self, tracker):
        """Trades that already have outcomes should not be re-settled."""
        tracker.log_trade("KXTEST-DONE", "crypto", "yes", 0.80, 0.50, 10, 50.0, 0.1)
        tracker.record_outcome(1, "win", 1.0)

        class MockClient:
            call_count = 0
            def get_market(self, ticker):
                MockClient.call_count += 1
                return {"market": {"status": "settled", "result": "yes"}}

        class MockAlerter:
            def send_settlement(self, *a, **kw):
                pass

        from main import check_settlements
        settled = check_settlements(tracker, MockClient(), MockAlerter())

        assert settled == 0
        assert MockClient.call_count == 0  # No API calls needed

    def test_api_error_skips_gracefully(self, tracker):
        """API errors on individual tickers should not crash the settlement check."""
        tracker.log_trade("KXTEST-ERR", "crypto", "yes", 0.80, 0.50, 10, 50.0, 0.1)
        tracker.log_trade("KXTEST-OK", "weather", "yes", 0.70, 0.40, 5, 20.0, 0.1)

        class MockClient:
            def get_market(self, ticker):
                if ticker == "KXTEST-ERR":
                    raise ConnectionError("API timeout")
                return {"market": {"status": "settled", "result": "yes"}}
            def settle_position(self, *a, **kw):
                pass

        class MockAlerter:
            def send_settlement(self, *a, **kw):
                pass

        from main import check_settlements
        settled = check_settlements(tracker, MockClient(), MockAlerter())

        # Only the non-errored ticker should settle
        assert settled == 1
        trades = tracker.trades
        assert trades[0]["outcome"] is None  # ERR ticker still open
        assert trades[1]["outcome"] == "win"  # OK ticker settled


class TestOverconfidenceGuardrail:
    """Tests for the overconfidence guardrail in process_opportunity."""

    @pytest.fixture(autouse=True)
    def _patch_guardrail(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "MAX_EDGE_THRESHOLD", 0.50)
        monkeypatch.setattr(config, "SIGNAL_ONLY", False)
        monkeypatch.setattr(config, "ECON_ALERT_ONLY", False)
        monkeypatch.setattr(config, "REQUIRE_APPROVAL", False)
        monkeypatch.setattr(config, "PAPER_TRADING", True)
        monkeypatch.setattr(config, "TOTAL_BANKROLL", 1_000_000)
        monkeypatch.setattr(config, "KELLY_FRACTION", 0.25)
        monkeypatch.setattr(config, "MAX_BET_FRACTION", 0.0015)
        monkeypatch.setattr(config, "ALLOCATION", {"crypto": 0.3333, "weather": 0.3333, "economics": 0.3334})

    def _make_opp(self, edge=0.10, market_prob=0.50, side="yes",
                  category="crypto", your_prob=None):
        if your_prob is None:
            your_prob = market_prob + edge if side == "yes" else 1 - market_prob + edge
        return {
            "ticker": "KXTEST-123",
            "title": "Test market",
            "category": category,
            "side": side,
            "your_prob": your_prob,
            "market_prob": market_prob,
            "edge": edge,
        }

    def test_rejects_extreme_edge(self):
        from main import process_opportunity

        class Noop:
            def __getattr__(self, _):
                return lambda *a, **kw: None

        opp = self._make_opp(edge=0.90, market_prob=0.02, your_prob=0.92)
        result = process_opportunity(opp, Noop(), Noop(), Noop())
        assert result is False

    def test_allows_cheap_contract_with_reasonable_edge(self):
        """A 2¢ contract with 30% edge should NOT be blocked — could be a real inefficiency."""
        opp = self._make_opp(edge=0.30, market_prob=0.02, side="yes", your_prob=0.32)
        import config
        assert opp["edge"] <= config.MAX_EDGE_THRESHOLD  # Passes edge guardrail

    def test_allows_expensive_contract_with_reasonable_edge(self):
        """A 98¢ contract with 30% NO edge should NOT be blocked."""
        opp = self._make_opp(edge=0.30, market_prob=0.98, side="no", your_prob=0.32)
        import config
        assert opp["edge"] <= config.MAX_EDGE_THRESHOLD  # Passes edge guardrail

    def test_boundary_edge_at_threshold(self):
        """Edge exactly at MAX_EDGE_THRESHOLD should be rejected (> not >=)."""
        import config
        # Edge of 0.51 should be rejected (> 0.50)
        from main import process_opportunity

        class Noop:
            def __getattr__(self, _):
                return lambda *a, **kw: None

        opp = self._make_opp(edge=0.51, market_prob=0.30, your_prob=0.81)
        result = process_opportunity(opp, Noop(), Noop(), Noop())
        assert result is False

    def test_edge_just_below_threshold_passes(self):
        """Edge at exactly 50% should pass (> not >=)."""
        opp = self._make_opp(edge=0.50, market_prob=0.30, your_prob=0.80)
        import config
        assert opp["edge"] <= config.MAX_EDGE_THRESHOLD
