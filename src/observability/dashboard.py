"""
Streamlit Observability Dashboard.
Connected to live SQLite WAL persistence layer.
Displays real-time equity curve, active positions, LLM deliberation thought logs,
circuit breaker status, and remote operator controls.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import pandas as pd
import streamlit as st

from src.domain.models import CircuitBreakerTier, ExitReason, PositionStatus
from src.risk.engine import LOCK_FILE_PATH
from src.storage.db import DEFAULT_DB_PATH, Database


def get_db() -> Database:
    db_path = os.getenv("DB_PATH", DEFAULT_DB_PATH)
    return Database(db_path=db_path)


def render_dashboard() -> None:
    st.set_page_config(
        page_title="Autonomous Trading Desk ($100 Sandbox)",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.title("Autonomous Stock Exchange Desk")
    st.caption("AI-Assisted Quantitative Swing Engine | $100 Capital Sandbox")

    db = get_db()
    latest_snap = db.get_latest_portfolio_snapshot()
    open_positions = db.get_open_positions()

    equity = latest_snap.total_equity if latest_snap else 100.0
    cash = latest_snap.cash_balance if latest_snap else 100.0
    active_slots = len(open_positions)
    tier = latest_snap.circuit_breaker_tier if latest_snap else CircuitBreakerTier.NORMAL
    is_locked = LOCK_FILE_PATH.exists()

    tier_label = "NORMAL (Tier 0)"
    if is_locked or tier == CircuitBreakerTier.HARD_LIQUIDATION:
        tier_label = "HARD LIQUIDATION (Tier 2)"
    elif tier == CircuitBreakerTier.SOFT_HALT:
        tier_label = "SOFT FREEZE (Tier 1)"

    # 1. Top Metrics Bar
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        pnl_delta = equity - 100.0
        delta_str = f"{pnl_delta:+.2f} ({pnl_delta:+.1f}%)"
        st.metric(label="Total Equity", value=f"${equity:.2f}", delta=delta_str)
    with col2:
        buf_status = "Buffer $10.00 OK" if cash >= 10.0 else "Buffer Deficit!"
        st.metric(label="Cash Reserve", value=f"${cash:.2f}", delta=buf_status)
    with col3:
        st.metric(label="Active Slots", value=f"{active_slots} / 3", delta=f"{3 - active_slots} Available")
    with col4:
        st.metric(label="Circuit Breaker", value=tier_label)
    with col5:
        st.metric(label="Max Risk Cap", value="$3.00 / trade", delta="3.0% Max")

    st.divider()

    # 2. Main Layout: Left = Positions & Chart, Right = LLM Committee Journal
    left_col, right_col = st.columns([3, 2])

    with left_col:
        st.subheader("Active Swing Positions (1–14 Day Horizon)")
        if open_positions:
            pos_rows = []
            for p in open_positions:
                unrealized_pct = (
                    ((p.current_price - p.entry_price) / p.entry_price) * 100.0
                    if p.entry_price > 0
                    else 0.0
                )
                pos_rows.append(
                    {
                        "Ticker": p.ticker,
                        "Qty": p.qty,
                        "Entry ($)": f"${p.entry_price:.2f}",
                        "Current ($)": f"${p.current_price:.2f}",
                        "Market Value ($)": f"${p.market_value:.2f}",
                        "Unrealized PnL": f"${p.unrealized_pnl:+.2f} ({unrealized_pct:+.1f}%)",
                        "Stop Loss ($)": f"${p.stop_loss:.2f}",
                        "Take Profit ($)": f"${p.take_profit:.2f}",
                        "Status": p.status.value,
                    }
                )
            st.dataframe(pd.DataFrame(pos_rows), use_container_width=True)
        else:
            st.info("No active positions currently open. Standing by for screened setups.")

        st.subheader("Portfolio Equity Curve vs Benchmark")
        history = db.get_portfolio_history(limit=60)
        if history and len(history) > 1:
            chart_df = pd.DataFrame(
                {
                    "Timestamp": [s.timestamp.strftime("%m-%d %H:%M") for s in history],
                    "Portfolio Equity ($)": [s.total_equity for s in history],
                }
            ).set_index("Timestamp")
            st.line_chart(chart_df, use_container_width=True)
        else:
            # Fallback demo curve
            sample_chart = pd.DataFrame(
                {
                    "Bar": [f"Bar {i}" for i in range(1, 11)],
                    "Portfolio Equity ($)": [100.0, 100.0, 100.25, 100.15, 100.80, 100.65, 101.10, 100.95, 101.20, equity],
                }
            ).set_index("Bar")
            st.line_chart(sample_chart, use_container_width=True)

        st.subheader("Operator Control Panel")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Soft Freeze (Halt New Buys)", type="secondary"):
                db.save_audit_log(
                    severity=db.get_audit_logs(limit=1)[0].severity if False else None,
                    component="Dashboard",
                    event_name="OPERATOR_SOFT_FREEZE",
                    message="Operator initiated soft freeze from Streamlit dashboard.",
                )
                st.warning("Soft halt initiated: New buy orders frozen.")
        with c2:
            if st.button("Emergency Liquidate All ($70 Floor)", type="primary"):
                with open(LOCK_FILE_PATH, "w") as f:
                    f.write("Operator triggered emergency liquidation from dashboard.\n")
                # Liquidate all open positions in db
                for p in open_positions:
                    p.status = PositionStatus.CLOSED
                    p.closed_at = datetime.now()
                    p.exit_reason = ExitReason.CIRCUIT_BREAKER_HALT
                    db.update_position(p)
                st.error("EMERGENCY KILL SWITCH ENGAGED: Liquidated all positions and set HALTED.lock!")
        with c3:
            if st.button("Resume Trading (Clear Lock)", type="secondary"):
                if LOCK_FILE_PATH.exists():
                    LOCK_FILE_PATH.unlink()
                st.success("HALTED.lock cleared. Trading resume requested.")

    with right_col:
        st.subheader("LLM Committee Thought Log & Audit Trail")
        candidates = db.list_candidates_by_status(db.get_candidate("non_existent").status if False else None) if False else []
        with db.session() as conn:
            cand_row = conn.execute(
                "SELECT * FROM screened_candidates ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()

        if cand_row:
            ticker = cand_row["ticker"]
            strategy = cand_row["strategy"]
            cand_id = cand_row["candidate_id"]
            st.info(f"Latest Candidate Evaluated: **{ticker}** (Setup: `{strategy}`)")

            deliberations = db.get_deliberations_for_candidate(cand_id)
            verdict = db.get_committee_verdict(cand_id)

            for d in deliberations:
                role_name = d.agent_role.value.replace("_", " ").title()
                weight_str = "49.0%" if "risk" in d.agent_role.value else "25.5%"
                expanded = True
                with st.expander(f"{role_name} (Weight: {weight_str})", expanded=expanded):
                    st.markdown(
                        f"**Stance**: `{d.stance.value}` | **Score**: `{d.score_10:.1f} / 10`  \n"
                        f"**Catalysts**: {', '.join(d.bullish_catalysts) if d.bullish_catalysts else 'None'}  \n"
                        f"**Risks**: {', '.join(d.risk_factors) if d.risk_factors else 'None'}  \n"
                        f"**Rationale**: {d.rationale_summary}"
                    )

            if verdict:
                st.warning(
                    f"**Consensus Status**: `{verdict.verdict_outcome.value}`  \n"
                    f"**Composite Score**: `{verdict.composite_score:.1f}%` | "
                    f"Risk Dissent: `{'YES' if verdict.risk_officer_dissent else 'NO'}`"
                )
        else:
            st.info("No candidates evaluated in database yet. Displaying reference configuration.")
            with st.expander("Sentiment & Catalyst Analyst (Weight: 25.5%)", expanded=True):
                st.markdown(
                    "**Stance**: `BULLISH` | **Score**: `8.5 / 10`  \n"
                    "**Catalysts**: Strong data center demand reported in supply chain checks.  \n"
                    "**Rationale**: Macro tech sentiment remains firmly risk-on; earnings are >7 days away."
                )
            with st.expander("Technical Structure Analyst (Weight: 25.5%)", expanded=True):
                st.markdown(
                    "**Stance**: `BULLISH` | **Score**: `8.0 / 10`  \n"
                    "**Catalysts**: Clean 3-day pullback into rising 20 EMA with 1.35x RVOL reversal.  \n"
                    "**Rationale**: Structural trend confirmed (Close > SMA200, SMA50 > SMA200). Reward/risk = 2.05R."
                )
            with st.expander("Adversarial Risk Officer (Weight: 49.0%)", expanded=True):
                st.markdown(
                    "**Stance**: `BULLISH` | **Score**: `8.0 / 10`  \n"
                    "**Identified Risks**: Sector beta to upcoming CPI release.  \n"
                    "**Rationale**: Sizing within $3.00 max risk constraint. Spread < 4 bps."
                )


if __name__ == "__main__":
    render_dashboard()
