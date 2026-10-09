"""
Autonomous Stock Exchange Trading Desk ($100 Sandbox)
Main Application & CLI Orchestrator.

Modes:
  --run-once        Execute a single daily scan, deliberation, risk check, and order routing.
  --loop            Run continuous daily scheduling loop.
  --backtest-tier1  Run 5-10 year macro vectorized backtest.
  --backtest-tier2  Run event-driven historical replay stress test.
  --dashboard       Launch the Streamlit observability dashboard.
  --bot             Start the Telegram HITL bot daemon.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import pandas as pd
from dotenv import load_dotenv

from src.backtest.tier1_vectorized import Tier1VectorizedBacktester
from src.backtest.tier2_replay import Tier2HistoricalReplayEngine
from src.broker.base import AbstractBrokerAdapter
from src.broker.factory import get_broker_adapter
from src.broker.reconciliation import ReconciliationService
from src.data.pipeline import MarketDataPipeline
from src.domain.models import (
    AuditSeverity,
    CandidateStatus,
    CircuitBreakerTier,
    OrderState,
    VerdictOutcome,
)
from src.llm.committee import CacheMode, TriadLLMCommittee
from src.risk.engine import LOCK_FILE_PATH, RiskEngine
from src.screener.screener import QuantitativeScreener
from src.storage.db import DEFAULT_DB_PATH, Database

# Default high-liquidity universe
DEFAULT_UNIVERSE = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "XLK", "SMH"
]


class TradingDeskOrchestrator:
    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        broker_type: str = "simulated",
        cache_mode: CacheMode = CacheMode.RECORD_ON_MISS,
        universe: Optional[List[str]] = None,
    ):
        self.db = Database(db_path=db_path)
        self.broker_type = broker_type
        self.broker = get_broker_adapter(broker_type)
        self.risk_engine = RiskEngine(db=self.db, broker=self.broker)
        self.screener = QuantitativeScreener()
        self.committee = TriadLLMCommittee(db=self.db, cache_mode=cache_mode)
        self.pipeline = MarketDataPipeline()
        self.universe = universe or DEFAULT_UNIVERSE

    def run_daily_cycle(self) -> Dict[str, Any]:
        """Run one cycle under a singleton lease with startup reconciliation (fail closed)."""
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        if not self.db.acquire_run_lease(
            run_id, mode="paper" if self.broker_type != "simulated" else "simulation",
            trigger="MANUAL", lease_owner=f"pid{os.getpid()}",
        ):
            self.db.save_audit_log(
                severity=AuditSeverity.WARNING,
                component="Orchestrator",
                event_name="RUN_REJECTED_LEASE_HELD",
                message="Another run holds the singleton lease; aborting.",
            )
            return {"status": "REJECTED_LEASE_HELD"}
        try:
            recon = ReconciliationService(db=self.db, broker=self.broker).reconcile(run_id=run_id)
            if not recon.is_clean:
                msg = f"Reconciliation not clean ({recon.status}); new entries blocked"
                self.db.save_audit_log(
                    severity=AuditSeverity.CRITICAL,
                    component="Orchestrator",
                    event_name="RUN_BLOCKED_RECONCILIATION",
                    message=msg,
                )
                self.db.release_run_lease(run_id, "FAILED", error_message=msg)
                return {"status": "BLOCKED_RECONCILIATION", "run_id": run_id}
            result = self._execute_cycle()
            self.db.release_run_lease(run_id, "COMPLETED")
            result["run_id"] = run_id
            return result
        except Exception as exc:
            self.db.save_audit_log(
                severity=AuditSeverity.CRITICAL,
                component="Orchestrator",
                event_name="RUN_FAILED",
                message=f"{type(exc).__name__}: {exc}",
            )
            self.db.release_run_lease(run_id, "FAILED", error_message=f"{type(exc).__name__}: {exc}")
            raise

    def _execute_cycle(self) -> Dict[str, Any]:
        """
        Executes a single end-to-end trading desk cycle:
        1. Audit & Lock check
        2. Ingest market data & calculate indicators
        3. Check SPY macro regime
        4. Screen candidates (Trend Pullback & RSI Oversold)
        5. Triad LLM committee deliberation
        6. Risk engine validation & position sizing
        7. Order routing to broker
        8. Bracket watchdog & portfolio snapshot
        """
        cycle_start = datetime.now(timezone.utc)
        print(f"\n[{cycle_start.isoformat()}] --- STARTING TRADING DESK CYCLE ---")

        # 1. Lock check
        if self.risk_engine.is_hard_locked():
            print("🚨 System is HARD LOCKED (HALTED.lock present). Aborting cycle.")
            self.db.save_audit_log(
                severity=AuditSeverity.CRITICAL,
                component="Orchestrator",
                event_name="CYCLE_ABORTED_HARD_LOCK",
                message="Cycle aborted due to persistent HALTED.lock",
            )
            return {"status": "ABORTED_HARD_LOCK"}

        # 2. Ingest Data
        print(f"📥 Ingesting market data for universe ({len(self.universe)} tickers)...")
        universe_dfs = {}
        for ticker in self.universe:
            df = self.pipeline.fetch_daily_bars(ticker, days=365)
            if not df.empty:
                universe_dfs[ticker] = df

        spy_df = universe_dfs.get("SPY")
        if spy_df is None or spy_df.empty:
            print("⚠️ Warning: SPY benchmark data unavailable. Proceeding with caution.")

        # Compute technical indicators
        enriched_universe = {}
        for sym, df in universe_dfs.items():
            enriched_universe[sym] = self.pipeline.compute_indicators(df, spy_df=spy_df)

        # 3. Bracket Watchdog on Open Positions
        current_prices = {
            sym: float(df["close"].iloc[-1])
            for sym, df in enriched_universe.items()
            if not df.empty
        }
        exits = self.risk_engine.run_bracket_watchdog(current_prices)
        if exits:
            print(f"🎯 Watchdog executed {len(exits)} exits: {exits}")

        # 4. Check Circuit Breaker & Sizing Capacity
        bal = self.broker.get_account_balance()
        tier = self.risk_engine.evaluate_circuit_breaker(bal.equity)
        print(f"💰 Equity: ${bal.equity:.2f} | Cash: ${bal.cash:.2f} | Circuit Tier: {tier.name}")

        if tier == CircuitBreakerTier.HARD_LIQUIDATION:
            print("🚨 Tier 2 Hard Liquidation Floor breached! Executing emergency liquidation.")
            self.risk_engine.execute_emergency_liquidation()
            return {"status": "EMERGENCY_LIQUIDATED"}

        if tier == CircuitBreakerTier.SOFT_HALT:
            print("⏸️ Tier 1 Soft Freeze active (Equity <= $80.00). Skipping new order generation.")
            snap = self.risk_engine.record_portfolio_snapshot()
            return {"status": "SOFT_HALTED", "equity": bal.equity}

        open_positions = self.broker.get_positions()
        available_slots = 3 - len(open_positions)
        if available_slots <= 0:
            print("🔒 All 3 slots currently occupied. Skipping screening.")
            snap = self.risk_engine.record_portfolio_snapshot()
            return {"status": "SLOTS_FULL", "active_positions": len(open_positions)}

        # 5. Quantitative Screening
        print(f"🔍 Screening universe for setups (Available slots: {available_slots})...")
        candidates = self.screener.scan_universe(
            enriched_universe, spy_df=enriched_universe.get("SPY"), top_n=available_slots
        )
        print(f"🔎 Screened {len(candidates)} setup candidates: {[c.ticker for c in candidates]}")

        for cand in candidates:
            self.db.save_candidate(cand)

        # 6. Triad LLM Committee Deliberation
        approved_orders = []
        for cand in candidates:
            headlines = self.pipeline.fetch_news_headlines(cand.ticker)
            print(f"🧠 Deliberating candidate {cand.ticker} with Triad LLM Committee...")
            verdict, delibs = self.committee.deliberate(
                cand, df=enriched_universe.get(cand.ticker), headlines=headlines
            )
            print(
                f"   Outcome: {verdict.verdict_outcome.value} (Score: {verdict.composite_score:.1f}%) "
                f"Risk Dissent: {verdict.risk_officer_dissent}"
            )

            if verdict.verdict_outcome == VerdictOutcome.AUTO_APPROVED:
                # 7. Deterministic Risk Engine Validation & Broker Routing
                ok, order, order_res, msg = self.risk_engine.validate_and_route_order(cand)
                print(f"   ⚡ Order Routing: {'SUCCESS' if ok else 'REJECTED'} -> {msg}")
                if ok:
                    approved_orders.append(cand.ticker)
            elif verdict.verdict_outcome == VerdictOutcome.HITL_ESCALATED:
                print(f"   ⚖️ Escalated to Telegram for HITL Human Sign-Off: {cand.ticker}")

        # 8. Record final snapshot
        snap = self.risk_engine.record_portfolio_snapshot()
        print(f"🏁 Cycle complete. Portfolio Equity: ${snap.total_equity:.2f} | Fills: {approved_orders}\n")
        return {
            "status": "COMPLETED",
            "equity": snap.total_equity,
            "candidates_screened": len(candidates),
            "approved_orders": approved_orders,
        }


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Autonomous Stock Exchange Trading Desk ($100 Sandbox)")
    parser.add_argument("--run-once", action="store_true", help="Execute single daily trading cycle")
    parser.add_argument("--loop", action="store_true", help="Run continuous polling loop")
    parser.add_argument("--backtest-tier1", action="store_true", help="Run Tier 1 Vectorized Backtest")
    parser.add_argument("--backtest-tier2", action="store_true", help="Run Tier 2 Event-Driven Replay")
    parser.add_argument("--dashboard", action="store_true", help="Launch Streamlit Dashboard")
    parser.add_argument("--bot", action="store_true", help="Launch Telegram Bot Daemon")
    parser.add_argument("--broker", type=str, default="simulated", choices=["simulated", "alpaca", "etoro"], help="Broker adapter")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="SQLite DB path")

    args = parser.parse_args()

    if args.dashboard:
        print("🚀 Launching Streamlit Observability Dashboard...")
        dashboard_script = str(Path(__file__).parent / "observability" / "dashboard.py")
        subprocess.run([sys.executable, "-m", "streamlit", "run", dashboard_script])
        return

    if args.backtest_tier1:
        print("📈 Running Tier 1 Vectorized Macro Backtest (5-10 yrs)...")
        tester = Tier1VectorizedBacktester()
        pipeline = MarketDataPipeline()
        universe_dfs = {}
        for sym in ["SPY", "AAPL", "MSFT", "NVDA", "QQQ"]:
            df = pipeline.fetch_daily_bars(sym, days=365 * 3)
            if not df.empty:
                universe_dfs[sym] = df
        res = tester.run(universe_dfs, spy_df=universe_dfs.get("SPY"))
        print("\n--- TIER 1 BACKTEST RESULTS ---")
        print(f"Total Trades: {res.total_trades} | Win Rate: {res.win_rate * 100:.1f}%")
        print(f"Profit Factor: {res.profit_factor:.2f} | Expectancy: {res.expectancy_r:.2f}R")
        print(f"Sharpe Ratio: {res.sharpe_ratio:.2f} | Sortino: {res.sortino_ratio:.2f}")
        print(f"Max Drawdown: {res.max_drawdown_pct:.1f}% | Final Equity: ${res.final_equity:.2f}")
        print(f"Circuit Breaker Breaches: {res.circuit_breaker_breaches}")
        return

    if args.backtest_tier2:
        print("📊 Running Tier 2 Event-Driven Historical Replay...")
        db = Database(db_path=args.db)
        engine = Tier2HistoricalReplayEngine(db=db)
        pipeline = MarketDataPipeline()
        universe_dfs = {}
        for sym in ["SPY", "AAPL", "MSFT", "NVDA"]:
            df = pipeline.fetch_daily_bars(sym, days=180)
            if not df.empty:
                universe_dfs[sym] = df
        report = engine.run_replay(universe_dfs, spy_df=universe_dfs.get("SPY"))
        print("\n--- TIER 2 REPLAY REPORT ---")
        print(f"Regime: {report.regime_name} ({report.start_date} to {report.end_date})")
        print(f"Total Trades: {report.total_trades} | Win Rate: {report.win_rate * 100:.1f}%")
        print(f"Profit Factor: {report.profit_factor:.2f} | Max Drawdown: {report.max_drawdown_pct:.1f}%")
        print(f"Final Equity: ${report.final_equity:.2f}")
        return

    # Normal trading orchestrator
    desk = TradingDeskOrchestrator(db_path=args.db, broker_type=args.broker)

    if args.run_once:
        desk.run_daily_cycle()
        return

    if args.loop:
        print("🔁 Entering daily trading cycle loop (Press Ctrl+C to terminate)...")
        while True:
            desk.run_daily_cycle()
            time.sleep(3600 * 4)  # 4-hour cycle check
    else:
        # Default run once if no arguments passed
        desk.run_daily_cycle()


if __name__ == "__main__":
    main()
