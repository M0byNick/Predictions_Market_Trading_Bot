# Prediction-Market Arbitrage Bot (Kalshi ↔ Polymarket)

A paper-trading arbitrage system that finds the same real-world event listed on two
prediction markets, verifies the two contracts actually resolve identically, and prices the
spread between them.

**Author:** Nicholas Morihisa ([github.com/M0byNick](https://github.com/M0byNick) ·
[linkedin.com/in/nmorihisa](https://www.linkedin.com/in/nmorihisa/))

## At a glance

| | |
|---|---|
| **What it does** | Cross-venue arbitrage detection between Kalshi and Polymarket US |
| **Core problem** | Deciding whether two differently-worded contracts resolve on the same event |
| **Approach** | Sentence-transformer retrieval for candidates, then LLM adjudication against a strict JSON schema |
| **Language** | Python 3.11+ |
| **Stack** | Anthropic API, sentence-transformers, SQLite, Flask, WebSockets, Pydantic, NumPy, Docker |
| **Status** | Paper trading (v1). No live capital. |

## The actual hard problem

Spotting a price difference is easy. The difficulty is **resolution divergence**: two
contracts that read almost identically but settle on different sources or different
timestamps. "BTC above $100k at close" and "BTC above $100k at 23:59 UTC" are not the same
event, and neither is a market settling on Chainlink versus one settling on Coinbase. If you
treat divergent markets as equivalent, what looks like locked-in arbitrage is a 100% loss on
one leg.

So the matching layer, not the pricing layer, is where this system spends its effort.

## How matching works

Two stages, because neither alone is sufficient:

1. **Retrieval** — `mapping/embeddings.py` embeds contract text and pulls plausible
   cross-venue candidates. Cheap, high recall, no judgment.
2. **Adjudication** — `mapping/adjudicator.py` sends each candidate pair to Claude under a
   constrained JSON schema (`mapping/schema.py`) that forces a verdict of `yes` / `no` /
   `ambiguous` plus a `match_polarity` of `same` or `inverse`.

Polarity matters and is easy to get wrong. "Will Democrats win the Arizona governorship?" on
one venue and "Will Republicans win?" on the other are a valid arbitrage pair — they are
inverses, exactly one resolves YES — and they need a different sizing rule rather than being
discarded as a mismatch. Timestamp and settlement-source differences, by contrast, are
divergence and get rejected outright.

## Layout

```
src/arb_bot/
  ingest/      kalshi.py, polymarket_global.py — venue clients and market pulls
  mapping/     embeddings.py, adjudicator.py, schema.py, edge_cases.py
  signal/      spread.py, validate.py — spread computation and sanity gates
  risk/        limits.py — position and exposure caps
  executor/    paper.py — paper fills
  dashboard/   app.py, pnl.py — Flask dashboard and PnL accounting
  db.py, config.py, heartbeat.py, main.py
scripts/       19 operational scripts: collection, batch submission, auth checks,
               pair validation, settlement sweeps, cost estimation, model comparison
```

## Running it

```bash
pip install -e ".[dev]"
cp .env.example .env          # Kalshi and Anthropic credentials
python -m arb_bot.main
```

Containerized via the included `Dockerfile`. Linting with ruff, tests with pytest.

## Design notes

- **Adjudication is batched and cached.** Pair verdicts are persisted, so re-runs do not
  re-pay for the same judgment. `scripts/cost_estimate.py` and `scripts/compare_models.py`
  exist because the LLM call is the dominant marginal cost.
- **Paper only, deliberately.** The executor writes simulated fills. Going live is a
  separate decision that should follow a forward test, not precede one.
- **Heartbeat over hope.** `heartbeat.py` writes liveness that an external fleet monitor
  polls, so a silently dead process is visible rather than assumed healthy.
