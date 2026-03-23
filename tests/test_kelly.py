"""Unit tests for Kelly criterion position sizing."""
from __future__ import annotations
import sys
import os

# Ensure the project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


# ── Helpers to patch config values ──────────────────────────────────────────

@pytest.fixture(autouse=True)
def _patch_config(monkeypatch):
    """Set deterministic config values for all tests."""
    import config
    monkeypatch.setattr(config, "KELLY_FRACTION", 0.25)
    monkeypatch.setattr(config, "MAX_BET_FRACTION", 0.15)
    monkeypatch.setattr(config, "MIN_EDGE_THRESHOLD", 0.05)
    monkeypatch.setattr(config, "TOTAL_BANKROLL", 5000)
    monkeypatch.setattr(config, "ALLOCATION", {
        "crypto": 0.50, "weather": 0.30, "economics": 0.20,
    })


from kelly import kelly_size, category_bankroll, format_sizing_summary


# ── kelly_size ──────────────────────────────────────────────────────────────

class TestKellySize:

    def test_positive_edge_yes(self):
        """Standard YES bet with positive edge."""
        result = kelly_size(0.70, 0.55, 2500, "yes")
        assert result["action"] == "trade"
        assert result["side"] == "yes"
        assert result["edge"] > 0

    def test_positive_edge_no(self):
        """Standard NO bet with positive edge."""
        result = kelly_size(0.30, 0.55, 2500, "no")
        assert result["action"] == "trade"
        assert result["side"] == "no"
        assert result["edge"] > 0

    def test_zero_edge(self):
        """No edge → no trade."""
        result = kelly_size(0.50, 0.50, 2500, "yes")
        assert result["action"] == "no_trade"
        assert result["recommended_contracts"] == 0

    def test_negative_edge(self):
        """Negative edge → no trade."""
        result = kelly_size(0.40, 0.55, 2500, "yes")
        assert result["action"] == "no_trade"

    def test_edge_below_threshold(self):
        """Edge exists but below MIN_EDGE_THRESHOLD (5%) → no trade."""
        result = kelly_size(0.53, 0.50, 2500, "yes")
        assert result["action"] == "no_trade"
        assert "below minimum" in result["reason"]

    def test_edge_at_threshold(self):
        """Edge exactly at threshold should trade (>=)."""
        result = kelly_size(0.55, 0.50, 2500, "yes")
        assert result["action"] in ("trade", "skip")  # skip if position too small

    def test_max_bet_fraction_cap(self):
        """Capped fraction should never exceed MAX_BET_FRACTION."""
        result = kelly_size(0.99, 0.50, 2500, "yes")
        if result["action"] == "trade":
            assert result["capped_fraction"] <= 0.15

    def test_market_prob_near_one(self):
        """market_prob near 1.0 → Kelly denominator near 0, should handle gracefully."""
        result = kelly_size(0.99, 0.98, 2500, "yes")
        # Edge is 0.01 < threshold → no trade
        assert result["action"] == "no_trade"

    def test_market_prob_near_zero(self):
        """market_prob near 0 (cheap contract)."""
        result = kelly_size(0.20, 0.05, 2500, "yes")
        assert result["action"] == "trade"
        assert result["recommended_contracts"] > 0

    def test_market_prob_exactly_one(self):
        """market_prob = 1.0 → division safe."""
        result = kelly_size(1.0, 1.0, 2500, "yes")
        assert result["action"] == "no_trade"

    def test_small_bankroll_skip(self):
        """Very small bankroll → position < $5 → skip."""
        result = kelly_size(0.70, 0.55, 10, "yes")
        assert result["action"] == "skip"
        assert "too small" in result["reason"]

    def test_kelly_override(self):
        """kelly_override should replace config.KELLY_FRACTION."""
        full = kelly_size(0.70, 0.55, 2500, "yes")
        reduced = kelly_size(0.70, 0.55, 2500, "yes", kelly_override=0.10)
        if full["action"] == "trade" and reduced["action"] == "trade":
            assert reduced["recommended_contracts"] <= full["recommended_contracts"]

    def test_no_side_flips_probabilities(self):
        """NO side should flip both your_prob and market_prob."""
        # Buying NO when we think event prob is 0.30, market says 0.55
        # Internally: your_prob=0.70, market_prob=0.45
        result = kelly_size(0.30, 0.55, 2500, "no")
        assert result["action"] == "trade"
        assert result["your_prob"] == round(0.70, 4)
        assert result["market_prob"] == round(0.45, 4)

    def test_result_keys(self):
        """Trade result should have all expected keys."""
        result = kelly_size(0.70, 0.55, 2500, "yes")
        assert result["action"] == "trade"
        expected_keys = {"action", "side", "edge", "your_prob", "market_prob",
                         "full_kelly_fraction", "fractional_kelly", "capped_fraction",
                         "recommended_contracts", "recommended_usd",
                         "category_bankroll", "expected_value"}
        assert expected_keys.issubset(result.keys())


# ── category_bankroll ───────────────────────────────────────────────────────

class TestCategoryBankroll:

    def test_crypto_allocation(self):
        assert category_bankroll("crypto") == 2500

    def test_weather_allocation(self):
        assert category_bankroll("weather") == 1500

    def test_economics_allocation(self):
        assert category_bankroll("economics") == 1000

    def test_unknown_category(self):
        assert category_bankroll("forex") == 0

    def test_custom_bankroll(self):
        assert category_bankroll("crypto", total_bankroll=10000) == 5000


# ── format_sizing_summary ──────────────────────────────────────────────────

class TestFormatSizingSummary:

    def test_no_trade(self):
        result = {"action": "no_trade", "reason": "test", "edge": 0}
        summary = format_sizing_summary(result)
        assert "test" in summary

    def test_trade(self):
        result = {
            "action": "trade", "side": "yes", "your_prob": 0.7,
            "market_prob": 0.55, "edge": 0.15, "capped_fraction": 0.10,
            "recommended_contracts": 10, "recommended_usd": 55.0,
            "expected_value": 15.0,
        }
        summary = format_sizing_summary(result)
        assert "10 contracts" in summary
