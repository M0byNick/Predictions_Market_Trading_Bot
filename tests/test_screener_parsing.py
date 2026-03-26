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


# ── Phase 5: Crypto Sentiment Tests ──────────────────────────────────────────

class TestCryptoSentimentDrift:
    """Tests for CryptoScreener.sentiment_to_drift."""

    def test_extreme_fear(self):
        from screeners.crypto import CryptoScreener
        drift = CryptoScreener.sentiment_to_drift(10)
        assert drift > 0  # Contrarian bullish
        assert drift == 0.05  # Max drift at default config

    def test_fear(self):
        from screeners.crypto import CryptoScreener
        drift = CryptoScreener.sentiment_to_drift(35)
        assert 0 < drift < 0.05

    def test_neutral(self):
        from screeners.crypto import CryptoScreener
        assert CryptoScreener.sentiment_to_drift(50) == 0.0

    def test_greed(self):
        from screeners.crypto import CryptoScreener
        drift = CryptoScreener.sentiment_to_drift(65)
        assert drift < 0

    def test_extreme_greed(self):
        from screeners.crypto import CryptoScreener
        drift = CryptoScreener.sentiment_to_drift(90)
        assert drift == -0.05  # Max negative drift

    def test_boundary_25(self):
        from screeners.crypto import CryptoScreener
        # 25 is in the fear zone (25-44)
        drift = CryptoScreener.sentiment_to_drift(25)
        assert 0 < drift < 0.05

    def test_boundary_75(self):
        from screeners.crypto import CryptoScreener
        # 75 is in the greed zone (56-75)
        drift = CryptoScreener.sentiment_to_drift(75)
        assert drift < 0

    def test_drift_with_model(self):
        """Verify drift actually changes model probability."""
        import math
        from scipy.stats import norm
        spot, strike, vol, T = 100000, 95000, 0.5, 7 / 365.25

        # Zero drift
        d2_zero = (math.log(spot / strike) - 0.5 * vol**2 * T) / (vol * math.sqrt(T))
        prob_zero = norm.cdf(d2_zero)

        # Positive drift (fear → bullish)
        drift = 0.05
        d2_drift = (math.log(spot / strike) + (drift - 0.5 * vol**2) * T) / (vol * math.sqrt(T))
        prob_drift = norm.cdf(d2_drift)

        # Positive drift should increase P(above strike)
        assert prob_drift > prob_zero


# ── Contract Type Handling Tests ──────────────────────────────────────────────

class TestCryptoContractTypes:
    """Tests for bracket, greater, and less contract type handling."""

    @pytest.fixture(autouse=True)
    def _make_screener(self):
        from screeners.crypto import CryptoScreener
        self.screener = CryptoScreener.__new__(CryptoScreener)

    def test_prob_above_basic(self):
        """_prob_above with spot > strike should give high probability."""
        prob = self.screener._prob_above(spot=100000, strike=90000, vol=0.5, T=7/365.25, drift=0.0)
        assert prob > 0.7

    def test_prob_above_spot_below_strike(self):
        """_prob_above with spot < strike should give low probability."""
        prob = self.screener._prob_above(spot=80000, strike=100000, vol=0.5, T=7/365.25, drift=0.0)
        assert prob < 0.3

    def test_bracket_contract(self):
        """Bracket contract: P(in range) should be small for far-from-spot brackets."""
        market = {
            "ticker": "KXBTC-B59025",
            "title": "BTC price range",
            "strike_type": "between",
            "floor_strike": 59000,
            "cap_strike": 59999.99,
            "close_time": "2026-12-25T21:00:00Z",
            "last_price_dollars": "0.0300",
        }
        import config
        # spot=70000, so P(59000 < BTC < 60000) should be small
        result = self.screener._evaluate_market(market, spot=70000, vol=0.5, ticker="BTC")
        # Either no opportunity (model_prob close to market) or NO side
        # The key: model should NOT think this is 100% YES
        if result is not None:
            assert result["your_prob"] < 0.5  # Should not be high confidence YES

    def test_bracket_near_spot(self):
        """Bracket contract near spot should have meaningful probability."""
        market = {
            "ticker": "KXBTC-B70025",
            "title": "BTC price range",
            "strike_type": "between",
            "floor_strike": 69000,
            "cap_strike": 70999.99,
            "close_time": "2026-12-25T21:00:00Z",
            "last_price_dollars": "0.2500",
        }
        result = self.screener._evaluate_market(market, spot=70000, vol=0.5, ticker="BTC")
        # Near-spot bracket should have reasonable model_prob
        # We check via snapshot — if returned, model_prob should be > 5%
        if result is not None:
            assert result["your_prob"] > 0.05

    def test_less_than_contract(self):
        """Less-than contract: P(BTC < 50000) when spot=70000 should be low."""
        market = {
            "ticker": "KXBTC-T50000",
            "title": "BTC price",
            "strike_type": "less",
            "cap_strike": 50000,
            "close_time": "2026-12-25T21:00:00Z",
            "last_price_dollars": "0.0200",
        }
        result = self.screener._evaluate_market(market, spot=70000, vol=0.5, ticker="BTC")
        # Model should agree with market that P(<50000) is low
        # Should not generate a high-edge YES signal
        if result is not None:
            assert result["side"] == "no" or result["your_prob"] < 0.3

    def test_greater_than_contract(self):
        """Greater-than contract: P(BTC > 50000) when spot=70000 should be high."""
        from datetime import datetime, timezone, timedelta
        next_week = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%dT21:00:00Z")
        market = {
            "ticker": "KXBTC-T50000",
            "title": "BTC price",
            "strike_type": "greater",
            "floor_strike": 50000,
            "close_time": next_week,
            "last_price_dollars": "0.9500",
        }
        result = self.screener._evaluate_market(market, spot=70000, vol=0.5, ticker="BTC")
        # P(BTC > 50000) in 7 days when spot=70000 → very high
        # Edge vs 95¢ market is small, so may not trigger
        if result is not None:
            assert result["your_prob"] > 0.5

    def test_bracket_sum_less_than_one(self):
        """Adjacent brackets should sum to less than 1 (probabilistic consistency)."""
        # Two adjacent brackets
        p1 = self.screener._prob_above(70000, 60000, 0.5, 7/365.25, 0.0)
        p2 = self.screener._prob_above(70000, 65000, 0.5, 7/365.25, 0.0)
        p3 = self.screener._prob_above(70000, 70000, 0.5, 7/365.25, 0.0)
        p4 = self.screener._prob_above(70000, 75000, 0.5, 7/365.25, 0.0)

        bracket_60_65 = p1 - p2
        bracket_65_70 = p2 - p3
        bracket_70_75 = p3 - p4

        # Each bracket should be a reasonable probability
        assert 0 < bracket_60_65 < 1
        assert 0 < bracket_65_70 < 1
        assert 0 < bracket_70_75 < 1
        # Sum should be less than 1
        assert bracket_60_65 + bracket_65_70 + bracket_70_75 < 1.0


# ── Weather Contract Type Tests ───────────────────────────────────────────────

class TestWeatherContractTypes:
    """Tests for weather bracket, less-than, and greater-than contracts."""

    @pytest.fixture(autouse=True)
    def _make_screener(self):
        from screeners.weather import WeatherScreener
        self.screener = WeatherScreener.__new__(WeatherScreener)
        self.screener._cycle_id = None

    def _make_weather_market(self, strike_type, floor=None, cap=None,
                              threshold=65, market_price="0.2500"):
        from datetime import datetime, timezone, timedelta
        # Use tomorrow to ensure days_out is valid (0-14 range)
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT21:00:00Z")
        m = {
            "ticker": "KXHIGHCHI-TEST",
            "title": f"Will the high temp be {threshold}?",
            "subtitle": f"{threshold}°",
            "close_time": tomorrow,
            "last_price_dollars": market_price,
            "strike_type": strike_type,
        }
        if floor is not None:
            m["floor_strike"] = floor
        if cap is not None:
            m["cap_strike"] = cap
        return m

    def _make_forecast(self, target_high=72):
        from datetime import datetime, timezone, timedelta
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT06:00:00-05:00")
        return [{"startTime": tomorrow,
                 "isDaytime": True, "temperature": target_high}]

    def test_bracket_near_forecast(self):
        """Bracket containing forecast should have high probability."""
        market = self._make_weather_market("between", floor=70, cap=74, market_price="0.1000")
        forecast = self._make_forecast(target_high=72)
        result = self.screener._evaluate_market(market, forecast, "CHI")
        # Bracket [70-74] centered on 72°F forecast should have significant prob
        assert result is not None
        assert result["your_prob"] > 0.2

    def test_bracket_far_from_forecast(self):
        """Bracket far from forecast should have low probability."""
        market = self._make_weather_market("between", floor=50, cap=52, market_price="0.0200")
        forecast = self._make_forecast(target_high=72)
        result = self.screener._evaluate_market(market, forecast, "CHI")
        # Bracket [50-52] when forecast is 72 → tiny probability
        # Model should NOT generate a big YES edge
        if result is not None:
            assert result["side"] == "no" or result["your_prob"] < 0.1

    def test_less_than_contract(self):
        """Less-than when forecast is well above should be low prob."""
        market = self._make_weather_market("less", cap=60, market_price="0.0500")
        forecast = self._make_forecast(target_high=72)
        result = self.screener._evaluate_market(market, forecast, "CHI")
        # P(high < 60) when forecast is 72 → very low
        if result is not None:
            assert result["side"] == "no" or result["your_prob"] < 0.1

    def test_greater_than_contract(self):
        """Greater-than below forecast should be high prob."""
        market = self._make_weather_market("greater", floor=65, market_price="0.8000")
        forecast = self._make_forecast(target_high=72)
        result = self.screener._evaluate_market(market, forecast, "CHI")
        # P(high > 65) when forecast is 72 → very high
        if result is not None:
            assert result["side"] == "yes"


# ── Weather Tail Dampening Tests ──────────────────────────────────────────────

class TestWeatherTailDampening:
    """Tests for tail dampening and market-informed blending."""

    @pytest.fixture(autouse=True)
    def _make_screener(self):
        from screeners.weather import WeatherScreener
        self.screener = WeatherScreener.__new__(WeatherScreener)
        self.screener._cycle_id = None

    def _tomorrow_str(self, fmt="close"):
        from datetime import datetime, timezone, timedelta
        t = datetime.now(timezone.utc) + timedelta(days=1)
        if fmt == "close":
            return t.strftime("%Y-%m-%dT21:00:00Z")
        return t.strftime("%Y-%m-%dT06:00:00-05:00")

    def _make_forecast(self, target_high=72):
        return [{"startTime": self._tomorrow_str("forecast"),
                 "isDaytime": True, "temperature": target_high}]

    def test_tail_bracket_dampened(self):
        """Bracket far from forecast should be dampened heavily."""
        # Bracket [85-86] when forecast is 72°F → z ≈ 5+ sigma away
        market = {
            "ticker": "KXHIGHCHI-B85.5",
            "title": "temp", "subtitle": "85°",
            "close_time": self._tomorrow_str(),
            "last_price_dollars": "0.0100",
            "strike_type": "between",
            "floor_strike": 85, "cap_strike": 86,
        }
        result = self.screener._evaluate_market(
            market, self._make_forecast(72), "CHI")
        # Should either be skipped (edge too small after dampening)
        # or have very low your_prob
        if result is not None:
            assert result["your_prob"] < 0.10

    def test_near_center_not_dampened(self):
        """Bracket near forecast should not be dampened much."""
        market = {
            "ticker": "KXHIGHCHI-B71.5",
            "title": "temp", "subtitle": "71°",
            "close_time": self._tomorrow_str(),
            "last_price_dollars": "0.2000",
            "strike_type": "between",
            "floor_strike": 71, "cap_strike": 73,
        }
        result = self.screener._evaluate_market(
            market, self._make_forecast(72), "CHI")
        # Near-center bracket should still have meaningful probability
        if result is not None:
            assert result["your_prob"] > 0.15

    def test_penny_market_blending(self):
        """At 1¢ market price, model should be blended toward market."""
        # Greater-than contract far from forecast
        market = {
            "ticker": "KXHIGHCHI-T90",
            "title": "temp", "subtitle": "90°",
            "close_time": self._tomorrow_str(),
            "last_price_dollars": "0.0100",
            "strike_type": "greater",
            "floor_strike": 90,
        }
        result = self.screener._evaluate_market(
            market, self._make_forecast(72), "CHI")
        # P(>90) when forecast=72 with dampening + blending → tiny
        if result is not None:
            assert result["edge"] < 0.10  # Edge should be very small

    def test_non_penny_no_blending(self):
        """Above 5¢, no market blending should occur."""
        market = {
            "ticker": "KXHIGHCHI-B71.5",
            "title": "temp", "subtitle": "71°",
            "close_time": self._tomorrow_str(),
            "last_price_dollars": "0.1500",
            "strike_type": "between",
            "floor_strike": 71, "cap_strike": 73,
        }
        result = self.screener._evaluate_market(
            market, self._make_forecast(72), "CHI")
        # At 15¢, no blending — model should drive the probability
        if result is not None:
            assert result["your_prob"] > 0.15


# ── Phase 5: Weather Dynamic Sigma Tests ─────────────────────────────────────

class TestWeatherDynamicSigma:
    """Tests for WeatherScreener.compute_dynamic_sigma."""

    def test_normal_spread(self):
        from datetime import date
        from screeners.weather import WeatherScreener
        gridpoint = {"2026-12-25": {"min": 55.0, "max": 72.0}}
        sigma = WeatherScreener.compute_dynamic_sigma(gridpoint, date(2026, 12, 25), 2)
        # spread = 17°F → σ ≈ 17/3.29 ≈ 5.17
        assert 4.5 < sigma < 6.0

    def test_tight_spread(self):
        from datetime import date
        from screeners.weather import WeatherScreener
        gridpoint = {"2026-12-25": {"min": 68.0, "max": 72.0}}
        sigma = WeatherScreener.compute_dynamic_sigma(gridpoint, date(2026, 12, 25), 0)
        # spread = 4°F → σ ≈ 1.22 → floor to 1.5
        assert sigma == 1.5

    def test_missing_date(self):
        from datetime import date
        from screeners.weather import WeatherScreener
        gridpoint = {"2026-12-25": {"min": 55.0, "max": 72.0}}
        sigma = WeatherScreener.compute_dynamic_sigma(gridpoint, date(2026, 12, 26), 3)
        assert sigma is None

    def test_none_gridpoint(self):
        from datetime import date
        from screeners.weather import WeatherScreener
        sigma = WeatherScreener.compute_dynamic_sigma(None, date(2026, 12, 25), 1)
        assert sigma is None

    def test_incomplete_entry(self):
        from datetime import date
        from screeners.weather import WeatherScreener
        gridpoint = {"2026-12-25": {"max": 72.0}}  # Missing min
        sigma = WeatherScreener.compute_dynamic_sigma(gridpoint, date(2026, 12, 25), 1)
        assert sigma is None

    def test_zero_spread(self):
        from datetime import date
        from screeners.weather import WeatherScreener
        gridpoint = {"2026-12-25": {"min": 70.0, "max": 70.0}}
        sigma = WeatherScreener.compute_dynamic_sigma(gridpoint, date(2026, 12, 25), 0)
        assert sigma is None  # spread <= 0


# ── Phase 5: Economics Quantitative Model Tests ──────────────────────────────

class TestEconModelProbability:
    """Tests for EconomicsScreener._model_probability."""

    @pytest.fixture(autouse=True)
    def _make_screener(self):
        from screeners.economics import EconomicsScreener
        self.screener = EconomicsScreener.__new__(EconomicsScreener)

    def _make_historical(self, values, changes=None):
        recent = values[:6]
        n = len(recent)
        mean = sum(recent) / n
        variance = sum((v - mean) ** 2 for v in recent) / (n - 1) if n > 1 else 0
        return {
            "latest": {"date": "2026-03-01", "value": values[0]},
            "previous": {"date": "2026-02-01", "value": values[1]} if len(values) > 1 else None,
            "mean_6m": mean,
            "std_6m": variance ** 0.5 if n > 1 else None,
            "min_6m": min(recent),
            "max_6m": max(recent),
            "changes": changes if changes else [values[i] - values[i+1] for i in range(len(values)-1)][:6],
            "trend": "stable",
            "values": [{"date": f"2026-{i:02d}-01", "value": v} for i, v in enumerate(values, 1)],
        }

    def test_cpi_change_above_threshold(self):
        # CPI changes: 0.3, 0.4, 0.3, 0.5, 0.2, 0.3
        hist = self._make_historical([3.5, 3.2, 2.8, 2.5, 2.0, 1.8, 1.5],
                                     changes=[0.3, 0.4, 0.3, 0.5, 0.2, 0.3])
        prob = self.screener._model_probability(hist, 0.2, "CPI")
        assert prob is not None
        assert 0.5 < prob < 1.0  # Mean change ~0.33, threshold 0.2 → high prob

    def test_cpi_change_below_threshold(self):
        hist = self._make_historical([3.5, 3.2, 2.8, 2.5, 2.0, 1.8, 1.5],
                                     changes=[0.3, 0.4, 0.3, 0.5, 0.2, 0.3])
        prob = self.screener._model_probability(hist, 0.8, "CPI")
        assert prob is not None
        assert prob < 0.3  # Mean ~0.33, threshold 0.8 → low prob

    def test_fed_rate_at_current(self):
        hist = self._make_historical([5.50, 5.50, 5.50, 5.50, 5.50, 5.50])
        prob = self.screener._model_probability(hist, 5.50, "FED_RATE")
        assert prob is not None
        assert 0.4 < prob < 0.6  # At current level → ~50%

    def test_fed_rate_above_current(self):
        hist = self._make_historical([5.50, 5.50, 5.50, 5.50, 5.50, 5.50])
        prob = self.screener._model_probability(hist, 6.00, "FED_RATE")
        assert prob is not None
        assert prob < 0.25  # 50bp above → unlikely at 1 month (σ=0.50)

    def test_unemployment_level(self):
        hist = self._make_historical([3.8, 3.7, 3.9, 3.6, 3.8, 3.7])
        prob = self.screener._model_probability(hist, 4.0, "UNEMPLOYMENT")
        assert prob is not None
        assert prob < 0.5  # Above mean → lower prob

    def test_insufficient_changes(self):
        hist = self._make_historical([3.5, 3.2], changes=[0.3])
        prob = self.screener._model_probability(hist, 0.3, "CPI")
        assert prob is None  # Needs 3+ changes

    def test_probability_monotone(self):
        """Higher threshold → lower P(above threshold)."""
        hist = self._make_historical([3.8, 3.7, 3.9, 3.6, 3.8, 3.7])
        p_low = self.screener._model_probability(hist, 3.5, "UNEMPLOYMENT")
        p_high = self.screener._model_probability(hist, 4.5, "UNEMPLOYMENT")
        assert p_low > p_high


# ── Phase 5: Economics Heuristic Flags Tests ─────────────────────────────────

class TestEconHeuristicFlags:
    """Tests for EconomicsScreener._heuristic_flags (supporting evidence)."""

    @pytest.fixture(autouse=True)
    def _make_screener(self):
        from screeners.economics import EconomicsScreener
        self.screener = EconomicsScreener.__new__(EconomicsScreener)

    def test_trend_rising_flag(self):
        hist = {"latest": {"value": 4.0}, "trend": "rising", "min_6m": 3.5, "max_6m": 4.0}
        flags = self.screener._heuristic_flags(hist, 3.8, 0.40)
        assert "trend_rising" in flags

    def test_cheap_tail_in_range(self):
        hist = {"latest": {"value": 4.0}, "trend": "stable", "min_6m": 3.5, "max_6m": 4.5}
        flags = self.screener._heuristic_flags(hist, 4.0, 0.05)
        assert "cheap_tail_in_range" in flags

    def test_near_threshold(self):
        hist = {"latest": {"value": 4.0}, "trend": "stable", "min_6m": 3.5, "max_6m": 4.5}
        flags = self.screener._heuristic_flags(hist, 4.01, 0.50)
        assert "near_threshold" in flags

    def test_no_flags_when_calm(self):
        hist = {"latest": {"value": 4.0}, "trend": "stable", "min_6m": 3.5, "max_6m": 4.5}
        flags = self.screener._heuristic_flags(hist, 5.0, 0.50)
        assert flags == []
