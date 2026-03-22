"""
Backtesting framework for validating screener probability estimates.

Fetches settled Kalshi markets and compares what our screeners WOULD have
predicted against actual outcomes. This validates edge estimates before
risking real capital.

Usage:
    python backtest.py                # Run backtest on all screener categories
    python backtest.py --category crypto   # Run on crypto only
    python backtest.py --days 90      # Look back 90 days (default: 30)

Output:
    - Brier score (calibration quality — lower is better, <0.25 is baseline)
    - Hit rate by edge bucket (are higher-edge trades actually winning more?)
    - Calibration table (predicted prob vs actual frequency)
    - Average edge realized vs predicted
"""
from __future__ import annotations
import sys
import json
import os
import math
from datetime import datetime, timezone, timedelta
from log import logger

# Use paper client to avoid needing real credentials
try:
    from paper_client import PaperClient
    from kalshi_client import KalshiClient
except ImportError:
    pass

from screeners.crypto import CryptoScreener
from screeners.weather import WeatherScreener
from screeners.economics import EconomicsScreener
from kelly import kelly_size, category_bankroll
import config

BACKTEST_CACHE = os.path.join("data", "backtest_cache.json")


def fetch_settled_markets(client, series_ticker: str, limit: int = 200) -> list:
    """Fetch settled (closed) markets from Kalshi for a given series."""
    try:
        result = client.get_markets(series_ticker=series_ticker, status="settled", limit=limit)
        return result.get("markets", [])
    except Exception as e:
        logger.warning("Failed to fetch settled markets for %s: %s", series_ticker, e)
        return []


def backtest_screener(screener, category: str, client, days: int = 30):
    """
    Run a screener's evaluation logic against settled markets.

    For each settled market:
      1. Record the actual outcome (yes_price == 100 means YES won)
      2. Ask the screener what it WOULD have predicted
      3. Compare predicted probability to actual outcome

    Returns a list of prediction records.
    """
    predictions = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Get the series tickers this screener covers
    series_map = getattr(screener, 'SERIES_MAP', {})

    for label, series in series_map.items():
        markets = fetch_settled_markets(client, series)
        logger.info("Backtest %s/%s: %d settled markets", category, label, len(markets))

        for market in markets:
            # Filter by date
            close_time_str = market.get("close_time", "")
            try:
                close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                if close_time < cutoff:
                    continue
            except Exception:
                continue

            # Determine actual outcome
            result_price = market.get("result", market.get("yes_price"))
            if result_price is None:
                continue

            # result == "yes" or yes_price == 100 means YES won
            if isinstance(result_price, str):
                actual_outcome = 1.0 if result_price.lower() == "yes" else 0.0
            else:
                actual_outcome = 1.0 if result_price >= 99 else 0.0

            # Get what the market was priced at (last trade price)
            market_prob = (market.get("last_price") or market.get("yes_ask") or 50) / 100.0

            # Try to get screener's prediction
            # For crypto, we need spot price and vol — use current values as rough proxy
            screener_prob = None
            try:
                if category == "crypto":
                    spot = screener._get_spot_price(label)
                    vol = screener._get_realized_vol(label)
                    if spot and vol:
                        opp = screener._evaluate_market(market, spot, vol, label)
                        if opp:
                            screener_prob = opp["your_prob"]
                elif category == "weather":
                    forecast = screener._get_nws_forecast(label)
                    if forecast:
                        opp = screener._evaluate_market(market, forecast, label)
                        if opp:
                            screener_prob = opp["your_prob"]
                elif category == "economics":
                    historical = screener._get_fred_data(label)
                    opp = screener._evaluate_market(market, historical, label)
                    if opp:
                        screener_prob = opp["your_prob"]
            except Exception:
                pass

            predictions.append({
                "ticker": market.get("ticker", ""),
                "title": market.get("title", ""),
                "category": category,
                "label": label,
                "market_prob": round(market_prob, 4),
                "screener_prob": round(screener_prob, 4) if screener_prob else None,
                "actual_outcome": actual_outcome,
                "close_time": close_time_str,
            })

    return predictions


def compute_metrics(predictions: list) -> dict:
    """Compute calibration and accuracy metrics from prediction records."""
    if not predictions:
        return {"error": "No predictions to evaluate"}

    # Separate predictions where screener had an opinion vs just market
    screener_preds = [p for p in predictions if p["screener_prob"] is not None]
    all_preds = predictions

    metrics = {
        "total_markets": len(all_preds),
        "screener_evaluated": len(screener_preds),
    }

    # Market Brier score (baseline — how well does the market predict?)
    market_brier = sum(
        (p["market_prob"] - p["actual_outcome"]) ** 2
        for p in all_preds
    ) / len(all_preds)
    metrics["market_brier_score"] = round(market_brier, 4)

    if screener_preds:
        # Screener Brier score
        screener_brier = sum(
            (p["screener_prob"] - p["actual_outcome"]) ** 2
            for p in screener_preds
        ) / len(screener_preds)
        metrics["screener_brier_score"] = round(screener_brier, 4)
        metrics["brier_improvement"] = round(market_brier - screener_brier, 4)

        # Edge analysis: did higher-edge predictions actually perform better?
        edge_buckets = {"0-5%": [], "5-10%": [], "10-15%": [], "15%+": []}
        for p in screener_preds:
            edge = abs(p["screener_prob"] - p["market_prob"])
            if edge < 0.05:
                edge_buckets["0-5%"].append(p)
            elif edge < 0.10:
                edge_buckets["5-10%"].append(p)
            elif edge < 0.15:
                edge_buckets["10-15%"].append(p)
            else:
                edge_buckets["15%+"].append(p)

        metrics["edge_buckets"] = {}
        for bucket, preds in edge_buckets.items():
            if preds:
                # For each prediction, did the screener's side win?
                wins = sum(
                    1 for p in preds
                    if (p["screener_prob"] > p["market_prob"] and p["actual_outcome"] == 1.0) or
                       (p["screener_prob"] < p["market_prob"] and p["actual_outcome"] == 0.0)
                )
                metrics["edge_buckets"][bucket] = {
                    "count": len(preds),
                    "win_rate": round(wins / len(preds), 4),
                }

        # Calibration table: group by predicted probability, compare to actual frequency
        cal_buckets = {}
        for p in screener_preds:
            bucket = round(p["screener_prob"] * 10) / 10  # round to nearest 10%
            bucket_key = f"{bucket:.0%}"
            if bucket_key not in cal_buckets:
                cal_buckets[bucket_key] = {"predictions": [], "outcomes": []}
            cal_buckets[bucket_key]["predictions"].append(p["screener_prob"])
            cal_buckets[bucket_key]["outcomes"].append(p["actual_outcome"])

        metrics["calibration"] = {}
        for bucket_key in sorted(cal_buckets.keys()):
            data = cal_buckets[bucket_key]
            avg_pred = sum(data["predictions"]) / len(data["predictions"])
            avg_outcome = sum(data["outcomes"]) / len(data["outcomes"])
            metrics["calibration"][bucket_key] = {
                "count": len(data["predictions"]),
                "avg_predicted": round(avg_pred, 4),
                "actual_frequency": round(avg_outcome, 4),
                "gap": round(avg_pred - avg_outcome, 4),
            }

    return metrics


def print_report(metrics: dict, category: str = "ALL"):
    """Print a human-readable backtest report."""
    print(f"\n{'═' * 60}")
    print(f"  BACKTEST REPORT: {category.upper()}")
    print(f"{'═' * 60}")

    print(f"\n  Markets analyzed:     {metrics.get('total_markets', 0)}")
    print(f"  Screener evaluated:   {metrics.get('screener_evaluated', 0)}")
    print(f"  Market Brier score:   {metrics.get('market_brier_score', 'N/A')}")

    if "screener_brier_score" in metrics:
        print(f"  Screener Brier score: {metrics['screener_brier_score']}")
        improvement = metrics.get("brier_improvement", 0)
        direction = "better" if improvement > 0 else "worse"
        print(f"  Brier improvement:    {improvement:+.4f} ({direction} than market)")

    if "edge_buckets" in metrics:
        print(f"\n  {'─' * 50}")
        print(f"  Edge Bucket Analysis (does higher edge = higher win rate?)")
        print(f"  {'─' * 50}")
        for bucket, data in metrics["edge_buckets"].items():
            print(f"  {bucket:>8s}: {data['count']:3d} trades, {data['win_rate']:.1%} win rate")

    if "calibration" in metrics:
        print(f"\n  {'─' * 50}")
        print(f"  Calibration (predicted vs actual)")
        print(f"  {'─' * 50}")
        print(f"  {'Bucket':>10s} {'Count':>6s} {'Predicted':>10s} {'Actual':>10s} {'Gap':>8s}")
        for bucket, data in metrics["calibration"].items():
            print(f"  {bucket:>10s} {data['count']:>6d} {data['avg_predicted']:>10.1%}"
                  f" {data['actual_frequency']:>10.1%} {data['gap']:>+8.1%}")

    print(f"\n{'═' * 60}\n")


def run_backtest(categories: list = None, days: int = 30):
    """Run backtests across specified categories."""
    try:
        from paper_client import PaperClient
        client = PaperClient()
    except Exception:
        client = KalshiClient()

    if categories is None:
        categories = ["crypto", "weather", "economics"]

    all_predictions = []

    for cat in categories:
        if cat == "crypto":
            screener = CryptoScreener(client)
        elif cat == "weather":
            screener = WeatherScreener(client)
        elif cat == "economics":
            screener = EconomicsScreener(client)
        else:
            continue

        logger.info("Running backtest for %s (last %d days)...", cat, days)
        preds = backtest_screener(screener, cat, client, days)
        all_predictions.extend(preds)

        if preds:
            cat_metrics = compute_metrics(preds)
            print_report(cat_metrics, cat)

    if len(categories) > 1 and all_predictions:
        overall = compute_metrics(all_predictions)
        print_report(overall, "ALL CATEGORIES")

    # Cache results
    os.makedirs(os.path.dirname(BACKTEST_CACHE), exist_ok=True)
    with open(BACKTEST_CACHE, "w") as f:
        json.dump({
            "run_time": datetime.now(timezone.utc).isoformat(),
            "days": days,
            "predictions": all_predictions,
        }, f, indent=2)
    logger.info("Backtest results cached to %s", BACKTEST_CACHE)


if __name__ == "__main__":
    days = 30
    categories = None

    args = sys.argv[1:]
    if "--days" in args:
        idx = args.index("--days")
        if idx + 1 < len(args):
            days = int(args[idx + 1])

    if "--category" in args:
        idx = args.index("--category")
        if idx + 1 < len(args):
            categories = [args[idx + 1]]

    run_backtest(categories=categories, days=days)
