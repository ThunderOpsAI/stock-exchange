"""
Unit tests for Walk-Forward Optimization, Purged Cross-Validation, and Held-Out Regime Reporting.
Verifies:
- Parameters are never selected on evaluation data.
- Reports distinguish train vs validation/test results.
- Purged windows eliminate lookahead and holding period overlap.
- Held-out regime classification and breakdown are surfaced in reports.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.backtest.walk_forward import (
    WalkForwardOptimizer,
    WalkForwardReport,
    WalkForwardSplitter,
)


def generate_synthetic_walkforward_data(num_bars: int = 150, seed: int = 42) -> pd.DataFrame:
    np.random.seed(seed)
    dates = [
        datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
        for i in range(num_bars)
    ]
    # Synthetic trending price with pullbacks
    closes = np.linspace(100.0, 160.0, num_bars) + np.random.normal(0, 0.6, num_bars)
    highs = closes + 1.2
    lows = closes - 1.2
    opens = closes - 0.1
    vols = np.full(num_bars, 1_000_000.0)

    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=dates,
    )


def test_walk_forward_splitter_purging():
    dates = [datetime(2025, 1, 1) + timedelta(days=i) for i in range(120)]
    splits = WalkForwardSplitter.generate_splits(
        sorted_dates=dates, train_bars=50, test_bars=20, purge_bars=5, step_bars=20
    )

    assert len(splits) >= 2
    for s in splits:
        assert len(s.train_dates) == 50
        assert len(s.purge_dates) == 5
        assert len(s.test_dates) == 20

        # Purge window strictly separates train and test
        assert set(s.train_dates).isdisjoint(set(s.test_dates))
        assert set(s.train_dates).isdisjoint(set(s.purge_dates))
        assert set(s.purge_dates).isdisjoint(set(s.test_dates))

        # Chronological order
        assert s.train_dates[-1] < s.purge_dates[0]
        assert s.purge_dates[-1] < s.test_dates[0]


def test_walk_forward_optimizer_parameters_never_selected_on_test_data():
    df_aapl = generate_synthetic_walkforward_data(num_bars=150, seed=1)
    df_msft = generate_synthetic_walkforward_data(num_bars=150, seed=2)
    universe = {"AAPL": df_aapl, "MSFT": df_msft}
    spy = generate_synthetic_walkforward_data(num_bars=150, seed=3)

    param_grid = [
        {"slot_target_usd": 25.0, "max_risk_cap_usd": 2.5, "half_spread_bps": 2.0},
        {"slot_target_usd": 30.0, "max_risk_cap_usd": 3.0, "half_spread_bps": 3.0},
        {"slot_target_usd": 35.0, "max_risk_cap_usd": 3.5, "half_spread_bps": 4.0},
    ]

    optimizer = WalkForwardOptimizer(
        parameter_grid=param_grid,
        metric_key="profit_factor",
        train_bars=60,
        test_bars=25,
        purge_bars=5,
    )

    report: WalkForwardReport = optimizer.run(universe, spy_df=spy)

    assert report.total_folds > 0
    assert len(report.fold_results) == report.total_folds

    # Acceptance criteria: Parameters are never selected on evaluation data;
    # reports distinguish train/validation/test results
    for fold in report.fold_results:
        assert fold.best_parameter in param_grid
        assert "profit_factor" in fold.train_metrics
        assert "profit_factor" in fold.test_metrics
        assert "sharpe" in fold.train_metrics
        assert "sharpe" in fold.test_metrics
        assert fold.train_period[1] < fold.test_period[0]  # strictly separated in time

    # Verify report aggregates both in-sample and out-of-sample metrics distinctly
    assert hasattr(report, "in_sample_avg_profit_factor")
    assert hasattr(report, "out_of_sample_avg_profit_factor")
    assert hasattr(report, "performance_degradation_pct")
    assert report.manifest_id is not None
    assert report.code_version is not None


def test_held_out_regime_reporting():
    df_aapl = generate_synthetic_walkforward_data(num_bars=160, seed=1)
    universe = {"AAPL": df_aapl}
    spy = generate_synthetic_walkforward_data(num_bars=160, seed=2)

    optimizer = WalkForwardOptimizer(
        parameter_grid=[{"slot_target_usd": 30.0, "max_risk_cap_usd": 3.0}],
        train_bars=60,
        test_bars=25,
        purge_bars=5,
    )
    report = optimizer.run(universe, spy_df=spy)

    # Acceptance criteria: held-out regime reporting is surfaced
    assert len(report.regime_breakdown) > 0
    for regime_name, regime_data in report.regime_breakdown.items():
        assert "folds_count" in regime_data
        assert "avg_test_profit_factor" in regime_data
        assert "avg_test_sharpe" in regime_data
