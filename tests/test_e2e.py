"""
End-to-End System Test for Autonomous Stock Exchange Trading Desk.
Simulates a full trading day: market data ingest -> quantitative screener ->
LLM Triad deliberation -> deterministic risk validation & sizing -> broker fill -> audit trail persistence.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from src.broker.simulated import SimulatedPaperBroker
from src.data.pipeline import MarketDataPipeline
from src.domain.models import CandidateStatus, OrderState, PositionStatus, StrategyType, VerdictOutcome
from src.llm.committee import CacheMode, TriadLLMCommittee
from src.main import TradingDeskOrchestrator
from src.risk.engine import RiskEngine
from src.screener.screener import QuantitativeScreener
from src.storage.db import Database


def build_synthetic_uptrend_bars(num_bars: int = 120, base_price: float = 100.0) -> pd.DataFrame:
    np.random.seed(101)
    dates = [
        datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
        for i in range(num_bars)
    ]
    closes = []
    p = base_price
    for _ in range(num_bars):
        p = p + 0.25 + np.random.normal(0, 0.5)
        closes.append(p)

    closes = np.array(closes)
    highs = closes + np.random.uniform(0.5, 1.2, size=num_bars)
    lows = closes - np.random.uniform(0.5, 1.2, size=num_bars)
    opens = closes + np.random.normal(0, 0.3, size=num_bars)
    volumes = np.random.uniform(1_000_000, 2_000_000, size=num_bars)

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=dates,
    )
    return df


@pytest.fixture
def e2e_env():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(db_path=path)

    fd_lock, path_lock = tempfile.mkstemp(suffix=".lock")
    os.close(fd_lock)
    os.remove(path_lock)
    lock_path = Path(path_lock)

    broker = SimulatedPaperBroker(initial_cash=100.0)
    risk_engine = RiskEngine(db=db, broker=broker, lock_file=lock_path)
    committee = TriadLLMCommittee(db=db, cache_mode=CacheMode.RECORD_ON_MISS)
    screener = QuantitativeScreener()

    yield db, broker, risk_engine, committee, screener, lock_path

    if os.path.exists(path):
        os.remove(path)
    if lock_path.exists():
        lock_path.unlink()


def test_full_trading_day_simulation(e2e_env):
    db, broker, risk_engine, committee, screener, _ = e2e_env

    # 1. Market Data Ingestion & Technical Indicator Suite
    spy_df = build_synthetic_uptrend_bars(num_bars=120, base_price=450.0)
    spy_enriched = MarketDataPipeline.compute_indicators(spy_df)

    nvda_df = build_synthetic_uptrend_bars(num_bars=120, base_price=120.0)
    nvda_enriched = MarketDataPipeline.compute_indicators(nvda_df, spy_df=spy_enriched)

    # Force NVDA last bar to be a valid Trend Pullback reversal setup
    ema20 = nvda_enriched["ema_20"].iloc[-1]
    nvda_enriched.iloc[-2, nvda_enriched.columns.get_loc("low")] = ema20 * 0.995
    nvda_enriched.iloc[-2, nvda_enriched.columns.get_loc("high")] = ema20 * 1.01
    nvda_enriched.iloc[-1, nvda_enriched.columns.get_loc("open")] = ema20 * 1.00
    nvda_enriched.iloc[-1, nvda_enriched.columns.get_loc("low")] = ema20 * 0.998
    nvda_enriched.iloc[-1, nvda_enriched.columns.get_loc("high")] = ema20 * 1.05
    nvda_enriched.iloc[-1, nvda_enriched.columns.get_loc("close")] = ema20 * 1.04
    nvda_enriched.iloc[-1, nvda_enriched.columns.get_loc("rvol_20")] = 1.45
    nvda_enriched.iloc[-1, nvda_enriched.columns.get_loc("rs_spy_63d")] = 1.20
    nvda_enriched.iloc[-1, nvda_enriched.columns.get_loc("volume")] = 2_500_000.0

    universe = {"NVDA": nvda_enriched}

    # 2. Macro Regime & Screener
    regime_ok, _ = screener.check_macro_regime(spy_enriched)
    assert regime_ok is True

    candidates = screener.scan_universe(universe, spy_df=spy_enriched, top_n=3)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.ticker == "NVDA"
    assert cand.strategy == StrategyType.TREND_PULLBACK
    assert cand.allocated_usd <= 30.0

    # Persist candidate
    db.save_candidate(cand)
    assert db.get_candidate(cand.candidate_id) is not None

    # 3. Triad LLM Committee Deliberation
    headlines = [{"title": "NVDA announces record GPU demand from cloud partners"}]
    verdict, delibs = committee.deliberate(cand, df=nvda_enriched, headlines=headlines)

    assert len(delibs) == 3
    assert verdict.composite_score >= 74.5
    assert verdict.verdict_outcome == VerdictOutcome.AUTO_APPROVED

    # Check persistence of deliberations and verdict
    stored_delibs = db.get_deliberations_for_candidate(cand.candidate_id)
    assert len(stored_delibs) == 3
    stored_verdict = db.get_committee_verdict(cand.candidate_id)
    assert stored_verdict is not None
    assert stored_verdict.verdict_outcome == VerdictOutcome.AUTO_APPROVED

    # 4. Deterministic Risk Engine & Broker Execution
    broker.set_price("NVDA", cand.entry_est)
    ok, order, order_res, msg = risk_engine.validate_and_route_order(cand)

    assert ok is True
    assert order.state == OrderState.FILLED
    assert order_res.status == OrderState.FILLED

    # Check database order and position records
    stored_order = db.get_order(order.order_id)
    assert stored_order.state == OrderState.FILLED
    assert stored_order.allocated_usd == pytest.approx(cand.allocated_usd, abs=0.05)

    open_pos = db.get_open_positions()
    assert len(open_pos) == 1
    assert open_pos[0].ticker == "NVDA"
    assert open_pos[0].status == PositionStatus.OPEN

    # 5. Portfolio Snapshot & Invariant Checks
    snap = risk_engine.record_portfolio_snapshot()
    assert snap.total_equity == 100.0
    assert snap.cash_balance >= 10.0  # $10 cash buffer preserved!
    assert snap.active_slots_used == 1
    assert snap.circuit_breaker_tier.value == 0

    # 6. Bracket Watchdog: price advances to take-profit
    broker.set_price("NVDA", cand.take_profit + 1.0)
    exits = risk_engine.run_bracket_watchdog({"NVDA": cand.take_profit + 1.0})
    assert len(exits) == 1
    ticker, exit_reason, exit_price = exits[0]
    assert ticker == "NVDA"
    assert exit_reason == "TAKE_PROFIT"

    # Verify position is closed and cash increased
    assert len(broker.get_positions()) == 0
    final_bal = broker.get_account_balance()
    assert final_bal.cash > 100.0
    assert final_bal.realized_pnl > 0.0

    closed_pos = db.get_position(open_pos[0].position_id)
    assert closed_pos.status == PositionStatus.CLOSED
    assert closed_pos.exit_reason.value == "TAKE_PROFIT"


def test_orchestrator_daily_cycle(e2e_env, monkeypatch):
    db, broker, risk_engine, committee, screener, _ = e2e_env

    orchestrator = TradingDeskOrchestrator(
        db_path=db.db_path,
        broker_type="simulated",
        universe=["SPY", "AAPL"],
    )

    # Mock market data fetch to use synthetic data
    spy_df = build_synthetic_uptrend_bars(num_bars=100, base_price=450.0)
    aapl_df = build_synthetic_uptrend_bars(num_bars=100, base_price=150.0)

    def mock_fetch(ticker, **kwargs):
        return spy_df if ticker == "SPY" else aapl_df

    monkeypatch.setattr(orchestrator.pipeline, "fetch_daily_bars", mock_fetch)
    monkeypatch.setattr(orchestrator.pipeline, "fetch_news_headlines", lambda ticker: [])

    result = orchestrator.run_daily_cycle()
    assert result["status"] == "COMPLETED"
    assert result["equity"] > 0.0

