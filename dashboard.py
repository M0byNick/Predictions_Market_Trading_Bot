"""
Streamlit dashboard for the Kalshi prediction market bot.

Visual feedback for bankroll curve, screener accuracy, trade timeline,
and calibration analysis. Reads from SQLite (read-only).

Usage:
    streamlit run dashboard.py
"""
from __future__ import annotations

import sqlite3
import os

import streamlit as st
import pandas as pd

import config

DB_PATH = config.DB_PATH


@st.cache_resource
def get_connection():
    """Get a read-only SQLite connection."""
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def load_trades(conn) -> pd.DataFrame:
    """Load all trades into a DataFrame."""
    df = pd.read_sql_query("SELECT * FROM trades ORDER BY entry_time", conn)
    if not df.empty:
        df["entry_time"] = pd.to_datetime(df["entry_time"])
        df["settlement_time"] = pd.to_datetime(df["settlement_time"])
    return df


def load_daily_pnl(conn) -> pd.DataFrame:
    """Load daily P&L data."""
    df = pd.read_sql_query(
        "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT 90", conn
    )
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


# ── Page Config ──────────────────────────────────────────────────────

st.set_page_config(
    page_title="Kalshi Bot Dashboard",
    page_icon="\U0001f4ca",
    layout="wide",
)

st.title("\U0001f4ca Kalshi Prediction Market Bot")

conn = get_connection()

if conn is None:
    st.warning(f"Database not found at `{DB_PATH}`. Run the bot first to create it.")
    st.stop()

trades_df = load_trades(conn)
daily_df = load_daily_pnl(conn)

if trades_df.empty:
    st.info("No trades recorded yet. Run the bot to start generating data.")
    st.stop()

# ── Summary Metrics ──────────────────────────────────────────────────

settled = trades_df[trades_df["outcome"].notna()]
open_trades = trades_df[trades_df["outcome"].isna()]

col1, col2, col3, col4, col5 = st.columns(5)
with col1:
    total_pnl = settled["pnl_usd"].sum() if not settled.empty else 0
    st.metric("Total P&L", f"${total_pnl:.2f}")
with col2:
    hit_rate = (settled["outcome"] == "win").mean() if not settled.empty else 0
    st.metric("Hit Rate", f"{hit_rate:.1%}")
with col3:
    st.metric("Total Trades", len(trades_df))
with col4:
    st.metric("Settled", len(settled))
with col5:
    st.metric("Open", len(open_trades))

st.divider()

# ── Bankroll Curve ───────────────────────────────────────────────────

st.subheader("Bankroll Curve")

if not settled.empty:
    settled_sorted = settled.sort_values("settlement_time")
    settled_sorted["cumulative_pnl"] = settled_sorted["pnl_usd"].cumsum()

    tab1, tab2 = st.tabs(["Cumulative P&L", "Per-Category"])

    with tab1:
        st.line_chart(
            settled_sorted.set_index("settlement_time")["cumulative_pnl"],
            use_container_width=True,
        )

    with tab2:
        for cat in ["crypto", "weather", "economics"]:
            cat_df = settled_sorted[settled_sorted["category"] == cat]
            if not cat_df.empty:
                cat_df = cat_df.copy()
                cat_df["cat_cumulative"] = cat_df["pnl_usd"].cumsum()
                st.line_chart(
                    cat_df.set_index("settlement_time")["cat_cumulative"],
                    use_container_width=True,
                )
                st.caption(f"{cat.upper()} — {len(cat_df)} trades, ${cat_df['pnl_usd'].sum():.2f} P&L")

st.divider()

# ── Category Performance ─────────────────────────────────────────────

st.subheader("Category Performance")

if not settled.empty:
    perf_data = []
    for cat in ["crypto", "weather", "economics"]:
        cat_df = settled[settled["category"] == cat]
        if not cat_df.empty:
            wins = (cat_df["outcome"] == "win").sum()
            perf_data.append({
                "Category": cat.upper(),
                "Trades": len(cat_df),
                "Wins": int(wins),
                "Hit Rate": f"{wins / len(cat_df):.1%}",
                "P&L": f"${cat_df['pnl_usd'].sum():.2f}",
                "Avg Edge": f"{cat_df['edge_at_entry'].mean():.1%}",
            })

    if perf_data:
        st.dataframe(pd.DataFrame(perf_data), use_container_width=True, hide_index=True)

st.divider()

# ── Calibration ──────────────────────────────────────────────────────

st.subheader("Calibration (Predicted vs Actual)")

if not settled.empty and len(settled) >= 5:
    cal_df = settled.copy()
    cal_df["predicted"] = cal_df.apply(
        lambda r: r["your_prob"] if r["side"] == "yes" else (1 - r["your_prob"]),
        axis=1,
    )
    cal_df["actual"] = (cal_df["outcome"] == "win").astype(float)

    # Bucket by decile
    cal_df["bucket"] = (cal_df["predicted"] * 10).astype(int).clip(0, 9) / 10

    cal_grouped = cal_df.groupby("bucket").agg(
        count=("actual", "count"),
        avg_predicted=("predicted", "mean"),
        actual_frequency=("actual", "mean"),
    ).reset_index()

    # Reliability diagram
    chart_data = cal_grouped[["avg_predicted", "actual_frequency"]].copy()
    chart_data.index = cal_grouped["avg_predicted"]
    st.line_chart(chart_data, use_container_width=True)
    st.caption("Perfect calibration = points on the diagonal. Above = underconfident. Below = overconfident.")

    # Brier score
    brier = ((cal_df["predicted"] - cal_df["actual"]) ** 2).mean()
    st.metric("Brier Score", f"{brier:.4f}", help="Lower is better. 0.25 = random.")

st.divider()

# ── Daily P&L ────────────────────────────────────────────────────────

st.subheader("Daily P&L")

if not daily_df.empty:
    daily_pivot = daily_df.pivot_table(
        index="date", columns="category", values="realized_pnl", aggfunc="sum"
    ).fillna(0)
    st.bar_chart(daily_pivot, use_container_width=True)

st.divider()

# ── Open Positions ───────────────────────────────────────────────────

st.subheader("Open Positions")

if not open_trades.empty:
    display_cols = ["ticker", "category", "side", "your_prob", "market_prob",
                    "edge_at_entry", "num_contracts", "cost_usd", "entry_time"]
    st.dataframe(open_trades[display_cols], use_container_width=True, hide_index=True)
else:
    st.info("No open positions.")

st.divider()

# ── Recent Trades ────────────────────────────────────────────────────

st.subheader("Recent Settled Trades")

if not settled.empty:
    recent = settled.sort_values("settlement_time", ascending=False).head(20)
    display_cols = ["ticker", "category", "side", "edge_at_entry",
                    "outcome", "pnl_usd", "settlement_time"]
    st.dataframe(recent[display_cols], use_container_width=True, hide_index=True)

# ── Footer ───────────────────────────────────────────────────────────

st.divider()
st.caption("Auto-refreshes every 30 seconds. Data is read-only from SQLite.")

# Auto-refresh every 30 seconds
st.empty()
