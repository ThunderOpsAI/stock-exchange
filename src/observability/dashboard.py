"""
Streamlit Observability Dashboard.
Connected to live SQLite WAL persistence layer.
Displays:
- Run health and lifecycle tracking (trading_runs)
- Data freshness & market snapshots (market_snapshots)
- Broker reconciliation status & hash lineage (reconciliation_events)
- Protection status & native bracket / watchdog verification (protection_status)
- Complete decision deliberation lineage (screened_candidates -> deliberations -> verdicts -> orders)
- Real-time equity curve & active positions (zero demo/mock fallbacks; strictly persisted records)
- Operator controls (durable soft freeze, emergency liquidation, resume)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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


# ---------------------------------------------------------------------
# Data Extraction Helpers (Persisted Records Only, No Demo Values)
# ---------------------------------------------------------------------
def get_run_health_data(db: Database) -> Optional[Dict[str, Any]]:
    """Retrieves the most recent trading run record from persisted trading_runs table."""
    return db.get_last_run()


def get_reconciliation_data(db: Database) -> Optional[Dict[str, Any]]:
    """Retrieves the latest broker reconciliation event from persisted reconciliation_events."""
    return db.get_latest_reconciliation_event()


def get_protection_status_data(db: Database) -> List[Dict[str, Any]]:
    """Retrieves all active protection status records from persisted protection_status."""
    return db.get_active_protection_statuses()


def get_data_freshness_data(db: Database, limit: int = 20) -> List[Dict[str, Any]]:
    """Retrieves latest market snapshots with timestamps and freshness metrics."""
    query = """
    SELECT timestamp, ticker, close, rsi_14, ema_20, sma_50, sma_200, atr_14, spread_bps
    FROM market_snapshots
    ORDER BY timestamp DESC
    LIMIT ?
    """
    with db.session() as conn:
        rows = conn.execute(query, (limit,)).fetchall()
        results = []
        now = datetime.now(timezone.utc)
        for r in rows:
            ts_str = r["timestamp"]
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_secs = (now - ts).total_seconds()
            except Exception:
                age_secs = None
            results.append(
                {
                    "ticker": r["ticker"],
                    "timestamp": ts_str,
                    "age_seconds": round(age_secs, 1) if age_secs is not None else "N/A",
                    "close": r["close"],
                    "rsi_14": r["rsi_14"],
                    "ema_20": r["ema_20"],
                    "sma_50": r["sma_50"],
                    "sma_200": r["sma_200"],
                    "atr_14": r["atr_14"],
                    "spread_bps": r["spread_bps"],
                }
            )
        return results


def get_decision_lineage_data(db: Database, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Retrieves full decision lineage for recent candidates:
    Candidate -> Screened features -> Triad deliberations -> Committee verdict -> Order/Fill.
    """
    query = """
    SELECT candidate_id, ticker, strategy, entry_est, stop_loss, take_profit,
           risk_r, allocated_usd, rank_score, status, timestamp
    FROM screened_candidates
    ORDER BY timestamp DESC
    LIMIT ?
    """
    with db.session() as conn:
        candidates = conn.execute(query, (limit,)).fetchall()

    lineages: List[Dict[str, Any]] = []
    for c in candidates:
        cand_id = c["candidate_id"]
        delibs = db.get_deliberations_for_candidate(cand_id)
        verdict = db.get_committee_verdict(cand_id)

        # Find matching order if any
        with db.session() as conn:
            order_row = conn.execute(
                "SELECT order_id, side, state, allocated_usd, target_qty FROM orders WHERE candidate_id = ? LIMIT 1",
                (cand_id,),
            ).fetchone()

        lineages.append(
            {
                "candidate_id": cand_id,
                "ticker": c["ticker"],
                "strategy": c["strategy"],
                "entry_est": c["entry_est"],
                "stop_loss": c["stop_loss"],
                "take_profit": c["take_profit"],
                "risk_r": c["risk_r"],
                "allocated_usd": c["allocated_usd"],
                "rank_score": c["rank_score"],
                "candidate_status": c["status"],
                "timestamp": c["timestamp"],
                "deliberations": [
                    {
                        "role": d.agent_role.value,
                        "stance": d.stance.value,
                        "score_10": d.score_10,
                        "catalysts": d.bullish_catalysts,
                        "risks": d.risk_factors,
                        "rationale": d.rationale_summary,
                    }
                    for d in delibs
                ],
                "verdict": {
                    "verdict_outcome": verdict.verdict_outcome.value,
                    "composite_score": verdict.composite_score,
                    "risk_officer_dissent": verdict.risk_officer_dissent,
                    "hitl_status": verdict.hitl_status.value if verdict.hitl_status else None,
                }
                if verdict
                else None,
                "order": dict(order_row) if order_row else None,
            }
        )
    return lineages


# ---------------------------------------------------------------------
# Dashboard UI Renderers
# ---------------------------------------------------------------------
def render_dashboard() -> None:
    st.set_page_config(
        page_title="Autonomous Trading Desk ($100 Sandbox)",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.title("Autonomous Stock Exchange Desk")
    st.caption("AI-Assisted Quantitative Swing Engine | $100 Capital Sandbox (Paper Trading Only)")

    db = get_db()
    latest_snap = db.get_latest_portfolio_snapshot()
    open_positions = db.get_open_positions()

    equity = latest_snap.total_equity if latest_snap else 100.0
    cash = latest_snap.cash_balance if latest_snap else 100.0
    active_slots = len(open_positions)
    tier = latest_snap.circuit_breaker_tier if latest_snap else CircuitBreakerTier.NORMAL
    is_locked = LOCK_FILE_PATH.exists()
    is_soft_frozen = db.is_soft_freeze_active()

    tier_label = "NORMAL (Tier 0)"
    if is_locked or tier == CircuitBreakerTier.HARD_LIQUIDATION:
        tier_label = "HARD LIQUIDATION (Tier 2)"
    elif is_soft_frozen or tier == CircuitBreakerTier.SOFT_HALT:
        tier_label = "SOFT FREEZE (Tier 1)"

    # 1. Top Metrics Bar (Persisted Metrics Only)
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
        freeze_detail = db.get_soft_freeze_details()
        status_txt = "ACTIVE" if is_soft_frozen else "OFF"
        delta_txt = freeze_detail.get("reason", "") if (is_soft_frozen and freeze_detail) else "Normal"
        st.metric(label="Persistent Soft Freeze", value=status_txt, delta=delta_txt)

    st.divider()

    # 2. Tabs for Modular Observability
    try:
        tabs = st.tabs([
            "📊 Portfolio & Positions",
            "⚖️ Decision Lineage & Deliberation",
            "🩺 Run Health & Protection",
            "🔄 Data Freshness & Reconciliation",
        ])
        if hasattr(tabs, "__len__") and len(tabs) >= 4:
            tab_ops, tab_lineage, tab_health, tab_recon = tabs[0], tabs[1], tabs[2], tabs[3]
        else:
            tab_ops = tab_lineage = tab_health = tab_recon = st.container()
    except (ValueError, TypeError, Exception):
        tab_ops = tab_lineage = tab_health = tab_recon = st.container()

    # Tab 1: Portfolio & Positions
    with tab_ops:
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
                st.info("No active positions currently open in database.")

            st.subheader("Portfolio Equity Curve")
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
                st.info("Insufficient historical snapshots recorded for equity curve (< 2 points in database).")

        with right_col:
            st.subheader("Operator Control Panel")
            c1, c2, c3 = st.columns(3)
            with c1:
                if st.button("Soft Freeze (Halt New Buys)", type="secondary"):
                    db.set_soft_freeze(enabled=True, actor="dashboard_operator", reason="Operator engaged soft freeze from dashboard")
                    st.warning("Soft halt engaged: New buy orders frozen in database.")
            with c2:
                if st.button("Emergency Liquidate All ($70 Floor)", type="primary"):
                    with open(LOCK_FILE_PATH, "w") as f:
                        f.write("Operator triggered emergency liquidation from dashboard.\n")
                    for p in open_positions:
                        p.status = PositionStatus.CLOSED
                        p.closed_at = datetime.now(timezone.utc)
                        p.exit_reason = ExitReason.CIRCUIT_BREAKER_HALT
                        db.update_position(p)
                    st.error("EMERGENCY KILL SWITCH ENGAGED: Liquidated open positions and created HALTED.lock!")
            with c3:
                if st.button("Resume Trading (Clear Locks)", type="secondary"):
                    db.set_soft_freeze(enabled=False, actor="dashboard_operator", reason="Operator resumed trading from dashboard")
                    if LOCK_FILE_PATH.exists():
                        LOCK_FILE_PATH.unlink()
                    st.success("HALTED.lock cleared and soft freeze released. Trading resumed.")

            st.subheader("Recent System Audit Logs")
            logs = db.get_audit_logs(limit=10)
            if logs:
                log_rows = [
                    {
                        "Timestamp": l.timestamp.strftime("%H:%M:%S") if hasattr(l.timestamp, "strftime") else str(l.timestamp),
                        "Severity": l.severity.value if hasattr(l.severity, "value") else str(l.severity),
                        "Component": l.component,
                        "Event": l.event_name,
                        "Message": l.message,
                    }
                    for l in logs
                ]
                st.dataframe(pd.DataFrame(log_rows), use_container_width=True)
            else:
                st.info("No audit logs found in database.")

    # Tab 2: Decision Lineage & Deliberation
    with tab_lineage:
        st.subheader("Candidate Deliberation Lineage")
        lineages = get_decision_lineage_data(db, limit=10)
        if lineages:
            for lin in lineages:
                with st.expander(
                    f"Candidate {lin['ticker']} ({lin['strategy']}) — Status: {lin['candidate_status']} | ID: {lin['candidate_id']}",
                    expanded=False,
                ):
                    col_info1, col_info2, col_info3 = st.columns(3)
                    with col_info1:
                        st.markdown(f"**Target Sizing**: `${lin['allocated_usd']:.2f}`")
                        st.markdown(f"**Entry Est**: `${lin['entry_est']:.2f}`")
                    with col_info2:
                        st.markdown(f"**Stop Loss**: `${lin['stop_loss']:.2f}`")
                        st.markdown(f"**Take Profit**: `${lin['take_profit']:.2f}`")
                    with col_info3:
                        st.markdown(f"**Risk R**: `${lin['risk_r']:.2f}`")
                        st.markdown(f"**Rank Score**: `{lin['rank_score']:.2f}`")

                    if lin["verdict"]:
                        st.markdown("---")
                        v = lin["verdict"]
                        st.markdown(
                            f"**Committee Verdict**: `{v['verdict_outcome']}` | **Composite Score**: `{v['composite_score']:.1f}%` | "
                            f"**Risk Dissent**: `{'YES' if v['risk_officer_dissent'] else 'NO'}` | **HITL Status**: `{v['hitl_status'] or 'N/A'}`"
                        )

                    if lin["deliberations"]:
                        st.markdown("**Analyst Cases:**")
                        for d in lin["deliberations"]:
                            role_title = d["role"].replace("_", " ").title()
                            st.markdown(
                                f"• **{role_title}** [{d['stance']} - {d['score_10']:.1f}/10]: {d['rationale']}"
                            )

                    if lin["order"]:
                        ord_info = lin["order"]
                        st.success(
                            f"**Order Linked**: Order ID `{ord_info['order_id']}` | Side: `{ord_info['side']}` | State: `{ord_info['state']}` | Qty: `{ord_info['target_qty']}`"
                        )
        else:
            st.info("No candidate deliberations recorded in database yet.")

    # Tab 3: Run Health & Protection Status
    with tab_health:
        c_run, c_prot = st.columns(2)
        with c_run:
            st.subheader("Singleton Run Health & Lifecycle")
            run = get_run_health_data(db)
            if run:
                st.markdown(f"**Run ID**: `{run.get('run_id')}`")
                st.markdown(f"**Status**: `{run.get('status')}`")
                st.markdown(f"**Current Phase**: `{run.get('current_phase') or 'N/A'}`")
                st.markdown(f"**Trigger Source**: `{run.get('trigger_source') or 'SCHEDULED'}`")
                st.markdown(f"**Heartbeat At**: `{run.get('heartbeat_at') or 'N/A'}`")
                st.markdown(f"**Started At**: `{run.get('started_at') or 'N/A'}`")
                st.markdown(f"**Completed At**: `{run.get('completed_at') or 'N/A'}`")
                if run.get("error_message"):
                    st.error(f"Error: {run.get('error_message')}")
            else:
                st.info("No trading runs recorded in database yet.")

        with c_prot:
            st.subheader("Broker Exit Protection Status")
            prots = get_protection_status_data(db)
            if prots:
                prot_rows = [
                    {
                        "Ticker": p.get("ticker"),
                        "Protection Mode": p.get("protection_mode"),
                        "Watchdog Healthy": "Yes" if p.get("watchdog_healthy") == 1 else "Degraded",
                        "Stop Leg ID": p.get("stop_loss_order_id") or "None",
                        "TP Leg ID": p.get("take_profit_order_id") or "None",
                        "Last Verified": str(p.get("last_verified_at", ""))[:19],
                        "Degradation Reason": p.get("degradation_reason") or "Clean",
                    }
                    for p in prots
                ]
                st.dataframe(pd.DataFrame(prot_rows), use_container_width=True)
            else:
                st.info("No protection status records found in database.")

    # Tab 4: Data Freshness & Reconciliation
    with tab_recon:
        c_fresh, c_rec = st.columns(2)
        with c_fresh:
            st.subheader("Market Data Freshness & Indicators")
            snaps = get_data_freshness_data(db, limit=15)
            if snaps:
                st.dataframe(pd.DataFrame(snaps), use_container_width=True)
            else:
                st.info("No market data snapshots recorded in database yet.")

        with c_rec:
            st.subheader("Broker State Reconciliation Lineage")
            recon = get_reconciliation_data(db)
            if recon:
                st.markdown(f"**Latest Reconciliation Event**: `{recon.get('event_id')}`")
                st.markdown(f"**Resolution Status**: `{recon.get('resolution_status')}`")
                st.markdown(f"**Recorded At**: `{recon.get('created_at')}`")
                st.markdown(f"**Local Hash**: `{recon.get('local_snapshot_hash')[:16]}...`")
                st.markdown(f"**Broker Hash**: `{recon.get('broker_snapshot_hash')[:16]}...`")
                mismatches = recon.get("mismatches_json")
                if mismatches and mismatches != "[]":
                    st.warning(f"**Mismatches**: {mismatches}")
                else:
                    st.success("Clean state: No broker/local discrepancies detected.")
            else:
                st.info("No broker reconciliation events recorded in database yet.")


if __name__ == "__main__":
    render_dashboard()
