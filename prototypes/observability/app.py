"""
Streamlit Observability Dashboard Prototype for Stock Exchange Bot
Connects directly to the SQLite persistence event store.
"""

from datetime import datetime
import json
import sqlite3
import pandas as pd
import streamlit as st


def get_db_connection(db_path: str = "data/trading.db"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def render_dashboard():
    st.set_page_config(
        page_title="Autonomous Trading Desk ($100 Sandbox)",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.title("Autonomous Stock Exchange Desk")
    st.caption("AI-Assisted Quantitative Swing Engine | eToro $100 Sandbox")

    # 1. Top Metrics Bar
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric(label="Total Equity", value="$101.42", delta="+$1.42 (+1.42%)")
    with col2:
        st.metric(label="Cash Reserve", value="$72.92", delta="Buffer $10.00 OK")
    with col3:
        st.metric(label="Active Slots", value="1 / 3", delta="2 Available")
    with col4:
        st.metric(label="Circuit Breaker", value="NORMAL", delta="Tier 0")
    with col5:
        st.metric(label="Max Risk Cap", value="$3.00 / trade", delta="3.0% Max")

    st.divider()

    # 2. Main Layout: Left = Positions & Chart, Right = LLM Committee Journal
    left_col, right_col = st.columns([3, 2])

    with left_col:
        st.subheader("Active Positions (1-14 Day Swing)")
        positions_data = [
            {
                "Ticker": "NVDA",
                "Shares": 0.2195,
                "Entry ($)": 128.50,
                "Current ($)": 130.40,
                "Market Value ($)": 28.62,
                "Unrealized PnL": "+$0.42 (+1.48%)",
                "Stop Loss ($)": 124.20,
                "Take Profit ($)": 137.10,
                "Hold (Days)": 2,
            }
        ]
        st.dataframe(pd.DataFrame(positions_data), use_container_width=True)

        st.subheader("Portfolio Equity Curve vs Benchmark")
        chart_data = pd.DataFrame(
            {
                "Trading Day": [f"Day {i}" for i in range(1, 11)],
                "Portfolio ($)": [
                    100.0,
                    100.0,
                    100.25,
                    100.15,
                    100.80,
                    100.65,
                    101.10,
                    100.95,
                    101.20,
                    101.42,
                ],
                "SPY Benchmark ($)": [
                    100.0,
                    100.1,
                    99.8,
                    99.9,
                    100.4,
                    100.2,
                    100.5,
                    100.6,
                    100.7,
                    100.9,
                ],
            }
        )
        st.line_chart(
            chart_data.set_index("Trading Day"), use_container_width=True
        )

        st.subheader("Emergency Control Panel")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Halt New Orders (Soft Freeze)", type="secondary"):
                st.warning("Soft halt initiated: New buy orders frozen.")
        with c2:
            if st.button("Emergency Liquidate All ($70 Floor)", type="primary"):
                st.error("EMERGENCY KILL SWITCH: Liquidating all positions!")

    with right_col:
        st.subheader("LLM Committee Thought Log & Audit Trail")
        st.info("Latest Candidate Evaluated: **NVDA** (Signal: TREND_PULLBACK)")

        with st.expander(
            "Sentiment & Catalyst Analyst (Weight: 25.5%)", expanded=True
        ):
            st.markdown(
                "**Stance**: `BULLISH` | **Score**: `8.5 / 10`  \n"
                "**Catalysts**: Strong data center demand reported in supply chain checks; positive tier-1 broker price target revision.  \n"
                "**Rationale**: Macro tech sentiment remains firmly risk-on; earnings are 38 days away, posing zero immediate binary event risk."
            )

        with st.expander(
            "Technical Structure Analyst (Weight: 25.5%)", expanded=True
        ):
            st.markdown(
                "**Stance**: `BULLISH` | **Score**: `8.0 / 10`  \n"
                "**Catalysts**: Clean 3-day pullback into rising 20 EMA; volume dried up on dip then expanded 1.35x RVOL on bullish reversal candle.  \n"
                "**Rationale**: Structural trend confirmed ($Close > 200 SMA$ and $50 SMA > 200 SMA$). Reward-to-risk ratio is 2.05R."
            )

        with st.expander(
            "Adversarial Risk Officer (Weight: 49.0%)", expanded=True
        ):
            st.markdown(
                "**Stance**: `VETO / DISSENT` | **Score**: `3.5 / 10`  \n"
                "**Identified Risks**: FOMC meeting rate announcement scheduled in 48 hours; semiconductor sector beta to interest rates is elevated.  \n"
                "**Rationale**: Setup is technically sound, but macro binary headline risk could gap through stop-loss."
            )

        st.warning(
            "**Consensus Status**: `HITL Deadlock (51% Bullish vs 49% Risk Dissent)`  \n"
            "Escalated to Telegram for human sign-off."
        )


if __name__ == "__main__":
    render_dashboard()
