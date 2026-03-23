"""Unit tests for screener title/threshold parsing functions."""
from __future__ import annotations
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _patch_config(monkeypatch):
    import config
    monkeypatch.setattr(config, "KALSHI_API_BASE", "https://example.com")
    monkeypatch.setattr(config, "KALSHI_EMAIL", "")
    monkeypatch.setattr(config, "KALSHI_PRIVATE_KEY_PATH", "/dev/null")
    monkeypatch.setattr(config, "MIN_EDGE_THRESHOLD", 0.05)


def _make_market(title="", subtitle="", ticker=""):
    return {"title": title, "subtitle": subtitle, "ticker": ticker}


# ── Crypto: _parse_strike ───────────────────────────────────────────────────

class TestCryptoParseStrike:
    """Tests for CryptoScreener._parse_strike."""

    @pytest.fixture(autouse=True)
    def _make_screener(self):
        from screeners.crypto import CryptoScreener
        # We only need the parsing method, not a real client
        self.screener = CryptoScreener.__new__(CryptoScreener)

    def test_dollar_with_commas(self):
        m = _make_market(title="Bitcoin above $100,000?")
        assert self.screener._parse_strike(m) == 100000.0

    def test_dollar_without_commas(self):
        m = _make_market(title="Will ETH be above $3500?")
        assert self.screener._parse_strike(m) == 3500.0

    def test_dollar_with_decimals(self):
        m = _make_market(title="SOL above $125.50?")
        assert self.screener._parse_strike(m) == 125.50

    def test_from_ticker_T_format(self):
        m = _make_market(ticker="KXBTC-26MAR28-T100000")
        assert self.screener._parse_strike(m) == 100000.0

    def test_subtitle_fallback(self):
        m = _make_market(title="Bitcoin weekly", subtitle="Above $95,000")
        assert self.screener._parse_strike(m) == 95000.0

    def test_no_price(self):
        m = _make_market(title="Will Bitcoin go up?")
        assert self.screener._parse_strike(m) is None

    def test_multiple_prices_takes_first(self):
        m = _make_market(title="BTC between $90,000 and $100,000")
        assert self.screener._parse_strike(m) == 90000.0

    def test_empty_title(self):
        m = _make_market(title="", subtitle="", ticker="")
        assert self.screener._parse_strike(m) is None


# ── Weather: _parse_threshold ───────────────────────────────────────────────

class TestWeatherParseThreshold:
    """Tests for WeatherScreener._parse_threshold."""

    @pytest.fixture(autouse=True)
    def _make_screener(self):
        from screeners.weather import WeatherScreener
        self.screener = WeatherScreener.__new__(WeatherScreener)

    def test_above_65_f(self):
        m = _make_market(title="NYC daily high above 65°F?")
        assert self.screener._parse_threshold(m) == 65.0

    def test_no_degree_symbol(self):
        m = _make_market(title="Chicago high above 80 F?")
        assert self.screener._parse_threshold(m) == 80.0

    def test_degrees_word(self):
        m = _make_market(title="Will it be above 90 degrees?")
        assert self.screener._parse_threshold(m) == 90.0

    def test_subtitle(self):
        m = _make_market(title="Weather", subtitle="Above 75°F on Mar 25")
        assert self.screener._parse_threshold(m) == 75.0

    def test_no_number(self):
        m = _make_market(title="Will it be hot?")
        assert self.screener._parse_threshold(m) is None

    def test_multiple_numbers_takes_first(self):
        m = _make_market(title="Between 60 and 70 degrees")
        assert self.screener._parse_threshold(m) == 60.0


# ── Weather: _parse_target_date ─────────────────────────────────────────────

class TestWeatherParseTargetDate:
    """Tests for WeatherScreener._parse_target_date."""

    @pytest.fixture(autouse=True)
    def _make_screener(self):
        from screeners.weather import WeatherScreener
        self.screener = WeatherScreener.__new__(WeatherScreener)

    def test_iso_close_time(self):
        from datetime import date
        m = {"close_time": "2026-03-25T21:00:00Z"}
        assert self.screener._parse_target_date(m) == date(2026, 3, 25)

    def test_iso_with_offset(self):
        from datetime import date
        m = {"close_time": "2026-03-25T17:00:00-04:00"}
        assert self.screener._parse_target_date(m) == date(2026, 3, 25)

    def test_missing_close_time(self):
        m = {}
        assert self.screener._parse_target_date(m) is None

    def test_expiration_time_fallback(self):
        from datetime import date
        m = {"expiration_time": "2026-04-01T21:00:00Z"}
        assert self.screener._parse_target_date(m) == date(2026, 4, 1)


# ── Economics: _parse_threshold ─────────────────────────────────────────────

class TestEconParseThreshold:
    """Tests for EconomicsScreener._parse_threshold."""

    @pytest.fixture(autouse=True)
    def _make_screener(self):
        from screeners.economics import EconomicsScreener
        self.screener = EconomicsScreener.__new__(EconomicsScreener)

    def test_percentage(self):
        m = _make_market(title="CPI above 3.5%?")
        assert self.screener._parse_threshold(m, "CPI") == 3.5

    def test_percentage_integer(self):
        m = _make_market(title="Unemployment above 4%?")
        assert self.screener._parse_threshold(m, "UNEMPLOYMENT") == 4.0

    def test_rate_decimal(self):
        m = _make_market(title="Fed rate at 5.25?")
        assert self.screener._parse_threshold(m, "FED_RATE") == 5.25

    def test_no_threshold(self):
        m = _make_market(title="Will the Fed cut rates?")
        assert self.screener._parse_threshold(m, "FED_RATE") is None

    def test_subtitle_percentage(self):
        m = _make_market(title="CPI release", subtitle="Above 3.2%")
        assert self.screener._parse_threshold(m, "CPI") == 3.2


# ── Economics: _compute_trend ───────────────────────────────────────────────

class TestEconComputeTrend:
    """Tests for EconomicsScreener._compute_trend."""

    @pytest.fixture(autouse=True)
    def _make_screener(self):
        from screeners.economics import EconomicsScreener
        self.screener = EconomicsScreener.__new__(EconomicsScreener)

    def test_rising(self):
        # Recent (first half) higher than older (second half)
        values = [110, 108, 100, 95, 90, 88]
        assert self.screener._compute_trend(values) == "rising"

    def test_falling(self):
        values = [88, 90, 95, 100, 108, 110]
        assert self.screener._compute_trend(values) == "falling"

    def test_stable(self):
        values = [100, 100, 100, 100, 100, 100]
        assert self.screener._compute_trend(values) == "stable"

    def test_insufficient_data(self):
        values = [100, 101]
        assert self.screener._compute_trend(values) == "insufficient_data"
