"""
Unit tests for realistic backtest cost modeling:
- Spread, market impact slippage, fixed and variable fees
- Partial fills and volume participation
- Overnight gap-through execution
- Corporate actions (stock splits, cash dividends, delistings)
- Proves costs directly affect results and surfaces assumptions in reports
"""

from datetime import datetime, timedelta, timezone
from typing import Dict

import numpy as np
import pandas as pd
import pytest

from src.backtest.costs import (
    CostModelConfig,
    ExecutionCostModel,
    ExecutionCostSummary,
)
from src.backtest.tier1_vectorized import Tier1VectorizedBacktester


def generate_scenario_bars(num_bars: int = 60, seed: int = 42) -> pd.DataFrame:
    np.random.seed(seed)
    dates = [
        datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
        for i in range(num_bars)
    ]
    # Create trending data with pullbacks
    closes = np.linspace(100.0, 130.0, num_bars) + np.random.normal(0, 0.5, num_bars)
    highs = closes + 1.5
    lows = closes - 1.5
    opens = closes - 0.2
    vols = np.full(num_bars, 1_000_000.0)

    # Incur an intentional gap-down on bar 25
    opens[25] = opens[24] - 8.0
    lows[25] = opens[25] - 2.0
    closes[25] = opens[25] - 1.0

    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=dates,
    )


def test_cost_model_entry_and_exit_math():
    config = CostModelConfig(
        half_spread_bps=5.0,  # 0.05%
        slippage_bps=5.0,     # 0.05%
        fixed_fee_per_trade_usd=0.50,
        variable_fee_pct=0.0002,
    )
    model = ExecutionCostModel(config)

    # Entry: raw $100.00, 10 shares
    # Spread + slippage = 10 bps = 0.10% -> $100.10
    exec_p, filled_qty, fee = model.calculate_entry_execution(
        raw_price=100.0, target_shares=10.0, bar_volume=100_000.0
    )
    assert exec_p == 100.10
    assert filled_qty == 10.0
    assert fee > 0.50  # fixed fee + variable fee

    assert model.summary.total_costs_usd > 0
    assert model.summary.total_spread_cost_usd > 0
    assert model.summary.total_slippage_cost_usd > 0
    assert model.summary.total_fees_usd > 0


def test_cost_model_overnight_gap_slippage():
    config = CostModelConfig(
        half_spread_bps=2.0,
        slippage_bps=2.0,
        enable_gap_fill_model=True,
    )
    model = ExecutionCostModel(config)

    # Normal stop loss: bar open is above stop, bar low breaches stop
    # Fills near stop price
    exit_p_norm, fee_norm, reason_norm = model.calculate_exit_execution(
        stop_price=95.0, bar_open=97.0, bar_low=94.0, shares=5.0, is_stop_loss=True
    )
    assert reason_norm == "STOP_LOSS"
    assert exit_p_norm < 95.0  # slight spread/slippage below 95.0
    assert exit_p_norm > 94.0

    # Overnight Gap: stock closed at 98 yesterday, but opened today at 91 (far below 95 stop)
    exit_p_gap, fee_gap, reason_gap = model.calculate_exit_execution(
        stop_price=95.0, bar_open=91.0, bar_low=90.0, shares=5.0, is_stop_loss=True
    )
    assert reason_gap == "GAP_STOP"
    # Gap fill model ensures exit is near 91.0 (actual opening trade), NOT 95.0
    assert exit_p_gap < 91.0
    assert model.summary.gap_exits_count == 1


def test_cost_model_corporate_actions():
    actions = [
        {"ticker": "AAPL", "type": "split", "ratio": 2.0, "date": "2025-01-10"},
        {"ticker": "AAPL", "type": "dividend", "amount": 0.50, "date": "2025-01-15"},
        {"ticker": "XYZ", "type": "delisting", "date": "2025-01-20"},
    ]
    config = CostModelConfig(corporate_actions=actions)
    model = ExecutionCostModel(config)

    # 1. 2:1 Split on 2025-01-10
    q, e, sl, tp, div, delist = model.process_corporate_actions_for_date(
        ticker="AAPL",
        current_date_str="2025-01-10",
        position_shares=10.0,
        entry_price=100.0,
        stop_loss=90.0,
        take_profit=120.0,
    )
    assert q == 20.0  # shares doubled
    assert e == 50.0   # entry halved
    assert sl == 45.0  # stop halved
    assert tp == 60.0  # take profit halved
    assert div == 0.0

    # 2. Dividend on 2025-01-15
    q2, e2, sl2, tp2, div2, delist2 = model.process_corporate_actions_for_date(
        ticker="AAPL",
        current_date_str="2025-01-15",
        position_shares=20.0,
        entry_price=50.0,
        stop_loss=45.0,
        take_profit=60.0,
    )
    assert div2 == 10.0  # 20 shares * $0.50

    # 3. Delisting on 2025-01-20
    _, _, _, _, _, delist3 = model.process_corporate_actions_for_date(
        ticker="XYZ",
        current_date_str="2025-01-20",
        position_shares=5.0,
        entry_price=10.0,
        stop_loss=8.0,
        take_profit=15.0,
    )
    assert delist3 == "DELISTED"


def test_costs_affect_backtest_results_and_surfaced_in_reports():
    bars_nvda = generate_scenario_bars(num_bars=80, seed=1)
    bars_msft = generate_scenario_bars(num_bars=80, seed=2)
    universe = {"NVDA": bars_nvda, "MSFT": bars_msft}
    spy = generate_scenario_bars(num_bars=80, seed=3)

    # 1. Run with ZERO costs
    zero_cost_config = CostModelConfig(
        half_spread_bps=0.0,
        slippage_bps=0.0,
        fixed_fee_per_trade_usd=0.0,
        variable_fee_pct=0.0,
        enable_gap_fill_model=False,
    )
    bt_frictionless = Tier1VectorizedBacktester(
        initial_capital=100.0,
        half_spread_bps=0.0,
        cost_config=zero_cost_config,
    )
    res_frictionless = bt_frictionless.run(universe, spy_df=spy)

    # 2. Run with REALISTIC costs (spread, slippage, fees, gap model)
    realistic_cost_config = CostModelConfig(
        half_spread_bps=6.0,
        slippage_bps=5.0,
        fixed_fee_per_trade_usd=1.00,
        variable_fee_pct=0.0005,
        enable_gap_fill_model=True,
    )
    bt_realistic = Tier1VectorizedBacktester(
        initial_capital=100.0,
        cost_config=realistic_cost_config,
    )
    res_realistic = bt_realistic.run(universe, spy_df=spy)

    # Acceptance criteria: Tests show the costs affect results; assumptions are surfaced in reports
    assert res_realistic.cost_summary is not None
    assert res_realistic.cost_assumptions is not None
    assert res_realistic.cost_assumptions["half_spread_bps"] == 6.0
    assert res_realistic.cost_assumptions["slippage_bps"] == 5.0
    assert res_realistic.cost_assumptions["fixed_fee_per_trade_usd"] == 1.00

    if res_frictionless.total_trades > 0:
        # Realistic costs must produce lower equity due to frictions
        assert res_realistic.final_equity < res_frictionless.final_equity
        assert res_realistic.cost_summary["total_costs_usd"] > 0
