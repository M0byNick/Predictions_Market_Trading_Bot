"""Unit tests for the paper trading client."""
from __future__ import annotations
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


@pytest.fixture(autouse=True)
def _patch_config(monkeypatch):
    import config
    monkeypatch.setattr(config, "KALSHI_API_BASE", "https://example.com")
    monkeypatch.setattr(config, "KALSHI_EMAIL", "")
    monkeypatch.setattr(config, "KALSHI_PRIVATE_KEY_PATH", "/dev/null")


@pytest.fixture
def paper_client(tmp_path, monkeypatch):
    """Create a PaperClient with a temp state file."""
    import paper_client as pc
    monkeypatch.setattr(pc, "PAPER_TRADES_FILE", str(tmp_path / "paper_trades.json"))
    from paper_client import PaperClient

    # Bypass parent __init__ (no real API key)
    client = PaperClient.__new__(PaperClient)
    client.base_url = "https://example.com"
    client.email = ""
    client.private_key = None
    client._has_api = False
    client._state = {
        "balance": 5000_00,  # $5000 in cents
        "orders": [],
        "positions": {},
        "next_order_id": 1,
    }
    return client


class TestPlaceOrder:

    def test_basic_fill(self, paper_client):
        result = paper_client.place_order("KXBTC-T100000", "yes", 10, price=55)
        order = result["order"]
        assert order["status"] == "filled"
        assert order["count"] == 10
        assert order["remaining_count"] == 0
        # Balance: 5000_00 - (55 * 10) = 4450_00
        assert paper_client._state["balance"] == 5000_00 - 550

    def test_position_tracking(self, paper_client):
        paper_client.place_order("KXBTC-T100000", "yes", 10, price=55)
        positions = paper_client.get_positions()
        assert len(positions["market_positions"]) == 1
        pos = positions["market_positions"][0]
        assert pos["ticker"] == "KXBTC-T100000"
        assert pos["count"] == 10

    def test_position_averaging(self, paper_client):
        paper_client.place_order("KXBTC-T100000", "yes", 10, price=50)
        paper_client.place_order("KXBTC-T100000", "yes", 10, price=60)
        pos = paper_client._state["positions"]["KXBTC-T100000_yes"]
        assert pos["count"] == 20
        assert pos["avg_price"] == 55.0  # (50*10 + 60*10) / 20

    def test_insufficient_balance(self, paper_client):
        paper_client._state["balance"] = 100  # $1.00
        result = paper_client.place_order("T1", "yes", 100, price=50)
        order = result["order"]
        # Should fill what it can afford: 100 // 50 = 2 contracts
        assert order["count"] == 2
        assert paper_client._state["balance"] == 0

    def test_zero_balance(self, paper_client):
        paper_client._state["balance"] = 0
        result = paper_client.place_order("T1", "yes", 10, price=50)
        assert result["order"]["status"] == "canceled"


class TestGetOrder:

    def test_found(self, paper_client):
        paper_client.place_order("T1", "yes", 5, price=50)
        result = paper_client.get_order("1")
        assert result["order"]["status"] == "filled"

    def test_not_found(self, paper_client):
        result = paper_client.get_order("999")
        assert result["order"]["status"] == "not_found"


class TestBalance:

    def test_initial(self, paper_client):
        assert paper_client.get_balance()["balance"] == 5000_00

    def test_after_order(self, paper_client):
        paper_client.place_order("T1", "yes", 10, price=50)
        assert paper_client.get_balance()["balance"] == 5000_00 - 500


class TestSettlement:

    def test_win_yes(self, paper_client):
        paper_client.place_order("T1", "yes", 10, price=60)
        pnl = paper_client.settle_position("T1", "yes", "yes")
        # Won: payout = 10 * 100 = 1000, cost = 60 * 10 = 600, pnl = 400 cents = $4.00
        assert pnl == 4.0

    def test_loss_yes(self, paper_client):
        paper_client.place_order("T1", "yes", 10, price=60)
        pnl = paper_client.settle_position("T1", "yes", "no")
        # Lost: pnl = -(60 * 10) = -600 cents = -$6.00
        assert pnl == -6.0

    def test_win_no(self, paper_client):
        paper_client.place_order("T1", "no", 10, price=40)
        pnl = paper_client.settle_position("T1", "no", "no")
        # Won: payout = 1000, cost = 400, pnl = 600 cents = $6.00
        assert pnl == 6.0

    def test_no_position(self, paper_client):
        pnl = paper_client.settle_position("T1", "yes", "yes")
        assert pnl == 0.0

    def test_balance_restored_on_settlement(self, paper_client):
        initial = paper_client._state["balance"]
        paper_client.place_order("T1", "yes", 10, price=60)
        paper_client.settle_position("T1", "yes", "yes")
        # Balance = initial - 600 + 600 + 400 (profit) = initial + 400
        assert paper_client._state["balance"] == initial + 400
