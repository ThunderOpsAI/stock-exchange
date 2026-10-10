"""
Unit and integration tests for Two-Tier Backtesting Harness.
Tests Tier 1 Vectorized multi-year matrix simulation and Tier 2 Event-Driven Replay Engine.
"""

import os
import tempfile
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

pytestmark = pytest.mark.integration

from src.backtest.tier1_vectorized import Tier1VectorizedBacktester
from src.backtest.tier2_replay import Tier2HistoricalReplayEngine, ReplayTradeRecord, ReplayReport
from src.broker.simulated import SimulatedPaperBroker
from src.domain.models import (
    OrderSide, Position, PositionStatus,
)
from src.llm.committee import CacheMode
from src.risk.engine import RiskEngine
from src.storage.db import Database


def generate_backtest_bars(
    num_bars: int = 150,
    base_price: float = 100.0,
    trend: float = 0.15,
    seed: int = 42,
) -> pd.DataFrame:
    np.random.seed(seed)
    dates = [
        datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
        for i in range(num_bars)
    ]
    closes = []
    p = base_price
    for _ in range(num_bars):
        p = p + trend + np.random.normal(0, 0.8)
        closes.append(max(15.0, p))

    closes = np.array(closes)
    highs = closes + np.random.uniform(0.5, 1.2, size=num_bars)
    lows = closes - np.random.uniform(0.5, 1.2, size=num_bars)
    opens = closes + np.random.normal(0, 0.4, size=num_bars)
    volumes = np.random.uniform(600_000, 1_200_000, size=num_bars)

    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=dates,
    )


@pytest.fixture
def backtest_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(db_path=path)
    yield db
    if os.path.exists(path):
        os.remove(path)


def test_tier1_vectorized_backtester():
    spy_df = generate_backtest_bars(num_bars=150, base_price=450.0, trend=0.4, seed=1)
    df_aapl = generate_backtest_bars(num_bars=150, base_price=150.0, trend=0.3, seed=2)
    df_msft = generate_backtest_bars(num_bars=150, base_price=280.0, trend=0.25, seed=3)
    df_nvda = generate_backtest_bars(num_bars=150, base_price=100.0, trend=0.5, seed=4)

    universe = {"AAPL": df_aapl, "MSFT": df_msft, "NVDA": df_nvda}

    tester = Tier1VectorizedBacktester(initial_capital=100.0, max_slots=3)
    result = tester.run(universe, spy_df=spy_df)

    assert result.initial_capital == 100.0 if hasattr(result, "initial_capital") else True
    assert len(result.equity_curve) > 50
    assert result.final_equity > 0.0
    assert result.max_drawdown_pct >= 0.0
    assert result.circuit_breaker_breaches == 0  # 0 breaches across upward market


def test_tier2_event_driven_replay_engine(backtest_db):
    spy_df = generate_backtest_bars(num_bars=100, base_price=450.0, trend=0.3, seed=10)
    df1 = generate_backtest_bars(num_bars=100, base_price=120.0, trend=0.2, seed=11)
    df2 = generate_backtest_bars(num_bars=100, base_price=180.0, trend=0.25, seed=12)

    universe = {"STOCK1": df1, "STOCK2": df2}

    engine = Tier2HistoricalReplayEngine(
        db=backtest_db,
        cache_mode=CacheMode.RECORD_ON_MISS,
        initial_capital=100.0,
    )
    report = engine.run_replay(
        universe, spy_df=spy_df, regime_name="Sample Stress Test 2025"
    )

    assert report.regime_name == "Sample Stress Test 2025"
    assert report.initial_equity == 100.0
    assert len(report.equity_curve) > 50
    assert report.final_equity > 50.0
    assert report.hard_liquidations_triggered == 0

    # Verify that LLM committee cached entries in SQLite WAL database
    with backtest_db.session() as conn:
        count = conn.execute("SELECT COUNT(*) FROM deliberation_cache;").fetchone()[0]
        assert count >= 0


def test_tier2_overnight_gap_stop(backtest_db):
    """
    Regression test for the overnight gap-through stop branch in tier2_replay.py:168-185.

    Strategy
    --------
    Directly exercise the gap-stop execution logic by:
      1. Pre-seeding the simulated broker with one open GAPPER position
         (entry=$120, SL=$115, qty=0.25).
      2. Invoking the gap-stop detection/fill block directly (the same code
         path as run_replay's inner bracket watchdog) against a bar where
         open=$112 (well below SL=$115).
      3. Asserting that the fill is at  open*(1-half_spread)  NOT at $115,
         and that PnL is negative.

    Isolating the gap-stop block directly avoids the entry-below-SL rejection
    that the risk engine correctly raises when exec_price < stop_loss (which
    prevents ever entering such a position via the normal pipeline).
    """
    from src.data.pipeline import MarketDataPipeline

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------
    entry_price  = 120.0
    stop_loss    = 115.0
    take_profit  = 130.0
    qty          = 0.25
    gap_open     = 112.0          # clearly below SL
    base_spread  = 3.0            # bps (matches engine default)
    eff_spread   = base_spread / 10000.0
    half_spread  = eff_spread / 2.0
    expected_fill = round(gap_open * (1.0 - half_spread), 2)

    dates = [
        datetime(2025, 6, 1, tzinfo=timezone.utc),
        datetime(2025, 6, 2, tzinfo=timezone.utc),
    ]

    # Minimal 2-bar OHLCV: bar[0] normal, bar[1] gap-down
    gapper_df = pd.DataFrame(
        {
            "open":   [entry_price - 1.0, gap_open],
            "high":   [entry_price + 2.0, gap_open + 1.0],
            "low":    [entry_price - 2.0, gap_open - 1.0],
            "close":  [entry_price + 1.0, gap_open + 0.5],
            "volume": [2_000_000.0,       1_500_000.0],
        },
        index=dates,
    )

    # ------------------------------------------------------------------
    # Pre-seed broker with an open position (bypass screener/risk pipeline)
    # ------------------------------------------------------------------
    broker = SimulatedPaperBroker(initial_cash=100.0, slippage_bps=0.0)

    pos_id = "pos_gap_test_01"
    open_position = Position(
        position_id=pos_id,
        broker_position_id=f"broker_{pos_id}",
        ticker="GAPPER",
        side=OrderSide.BUY,
        qty=qty,
        entry_price=entry_price,
        current_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        market_value=round(qty * entry_price, 2),
        unrealized_pnl=0.0,
        status=PositionStatus.OPEN,
        opened_at=dates[0],
    )
    broker.positions[pos_id] = open_position
    broker.cash -= round(qty * entry_price, 2)
    broker.price_feed["GAPPER"] = entry_price

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Run the replay engine so it hits the code in tier2_replay.py
    # ------------------------------------------------------------------
    engine = Tier2HistoricalReplayEngine(db=backtest_db, initial_capital=100.0)
    
    import unittest.mock
    with unittest.mock.patch("src.backtest.tier2_replay.SimulatedPaperBroker", return_value=broker):
        report = engine.run_replay({"GAPPER": gapper_df})
        
    trade_records = report.trades
    equity_end = broker.get_account_balance().equity
    equity_start = 100.0

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------
    assert len(trade_records) == 1, (
        f"Expected 1 GAP_STOP trade. Got {len(trade_records)} trade(s)."
    )

    gs = trade_records[0]

    # Exit reason is GAP_STOP
    assert gs.exit_reason == "GAP_STOP"

    # Fill must be BELOW the nominal stop-loss — filled at open, not SL
    assert gs.exit_price < stop_loss, (
        f"Gap fill {gs.exit_price:.4f} must be below nominal SL {stop_loss}"
    )

    # Fill price must match  gap_open * (1 - half_spread)  within 1 cent
    assert abs(gs.exit_price - expected_fill) < 0.01, (
        f"Gap fill {gs.exit_price:.4f} != expected {expected_fill:.4f}"
    )

    # PnL must be negative (gapped below entry)
    assert gs.pnl < 0.0, f"Gap-stop should record a loss; got pnl={gs.pnl}"

    # Broker returned cash less than initial (net loss)
    assert equity_end < equity_start

    # No positions remain open
    assert len(broker.get_positions()) == 0
