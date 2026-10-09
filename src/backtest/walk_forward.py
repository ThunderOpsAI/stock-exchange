"""
Walk-Forward Optimization, Purged Cross-Validation, and Held-Out Regime Reporting.
Implements:
- WalkForwardSplitter: Generates purged, non-overlapping train/test partitions to eliminate lookahead bias.
- WalkForwardOptimizer: Selects optimal parameter sets strictly on In-Sample (Train) data,
  then evaluates held-out performance on Out-of-Sample (Test) data.
- Held-Out Regime Reporting: Evaluates and reports performance across detected market regimes.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.backtest.manifest import DataManifest, get_current_git_version
from src.backtest.tier1_vectorized import BacktestResult, Tier1VectorizedBacktester


@dataclass
class WalkForwardWindow:
    fold_idx: int
    train_dates: List[Any]
    purge_dates: List[Any]
    test_dates: List[Any]

    @property
    def train_start(self) -> str:
        return str(self.train_dates[0])[:10] if self.train_dates else ""

    @property
    def train_end(self) -> str:
        return str(self.train_dates[-1])[:10] if self.train_dates else ""

    @property
    def test_start(self) -> str:
        return str(self.test_dates[0])[:10] if self.test_dates else ""

    @property
    def test_end(self) -> str:
        return str(self.test_dates[-1])[:10] if self.test_dates else ""


@dataclass
class FoldResult:
    fold_idx: int
    train_period: Tuple[str, str]
    test_period: Tuple[str, str]
    best_parameter: Dict[str, Any]
    train_metrics: Dict[str, Any]
    test_metrics: Dict[str, Any]
    test_regime: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WalkForwardReport:
    total_folds: int
    parameter_grid: List[Dict[str, Any]]
    fold_results: List[FoldResult]
    in_sample_avg_profit_factor: float
    out_of_sample_avg_profit_factor: float
    in_sample_avg_sharpe: float
    out_of_sample_avg_sharpe: float
    performance_degradation_pct: float
    regime_breakdown: Dict[str, Dict[str, Any]]
    manifest_id: Optional[str] = None
    code_version: Optional[Dict[str, str]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class WalkForwardSplitter:
    """
    Generates purged, leak-free walk-forward splits.
    Guarantees: Train and Test are separated by a purge window to eliminate overlapping holding periods.
    """

    @staticmethod
    def generate_splits(
        sorted_dates: List[Any],
        train_bars: int = 60,
        test_bars: int = 20,
        purge_bars: int = 5,
        step_bars: Optional[int] = None,
        expanding: bool = False,
    ) -> List[WalkForwardWindow]:
        splits: List[WalkForwardWindow] = []
        step = step_bars or test_bars
        n_dates = len(sorted_dates)
        total_window = train_bars + purge_bars + test_bars

        if n_dates < total_window:
            return []

        fold = 0
        start_idx = 0
        while start_idx + total_window <= n_dates:
            train_start = 0 if expanding else start_idx
            train_end = start_idx + train_bars
            purge_start = train_end
            purge_end = purge_start + purge_bars
            test_start = purge_end
            test_end = test_start + test_bars

            train_dates = sorted_dates[train_start:train_end]
            purge_dates = sorted_dates[purge_start:purge_end]
            test_dates = sorted_dates[test_start:test_end]

            splits.append(
                WalkForwardWindow(
                    fold_idx=fold,
                    train_dates=train_dates,
                    purge_dates=purge_dates,
                    test_dates=test_dates,
                )
            )
            fold += 1
            start_idx += step

        return splits


class WalkForwardOptimizer:
    """
    Executes walk-forward cross-validation:
    1. For each fold, searches parameter grid on In-Sample (Train) data ONLY.
    2. Freezes selected best parameter.
    3. Evaluates selected parameter on held-out Out-of-Sample (Test) data.
    4. Categorizes performance by market regime.
    """

    def __init__(
        self,
        parameter_grid: List[Dict[str, Any]],
        metric_key: str = "profit_factor",
        train_bars: int = 60,
        test_bars: int = 20,
        purge_bars: int = 5,
    ):
        self.parameter_grid = parameter_grid
        self.metric_key = metric_key
        self.train_bars = train_bars
        self.test_bars = test_bars
        self.purge_bars = purge_bars

    def _slice_universe(
        self, universe_dfs: Dict[str, pd.DataFrame], dates: List[Any]
    ) -> Dict[str, pd.DataFrame]:
        date_set = set(dates)
        sliced: Dict[str, pd.DataFrame] = {}
        for sym, df in universe_dfs.items():
            valid_idx = df.index.intersection(date_set)
            sliced[sym] = df.loc[valid_idx]
        return sliced

    def _detect_regime(self, spy_df: Optional[pd.DataFrame], test_dates: List[Any]) -> str:
        if spy_df is None or spy_df.empty:
            return "UNKNOWN"
        sub = spy_df.loc[spy_df.index.intersection(set(test_dates))]
        if sub.empty:
            return "UNKNOWN"
        ret = (sub["close"].iloc[-1] - sub["close"].iloc[0]) / sub["close"].iloc[0]
        if ret > 0.02:
            return "BULL_TREND"
        elif ret < -0.02:
            return "BEAR_CORRECTION"
        else:
            return "SIDEWAYS_CHOP"

    def run(
        self,
        universe_dfs: Dict[str, pd.DataFrame],
        spy_df: Optional[pd.DataFrame] = None,
        manifest: Optional[DataManifest] = None,
    ) -> WalkForwardReport:
        all_dates = sorted(list(set().union(*[df.index for df in universe_dfs.values()])))
        windows = WalkForwardSplitter.generate_splits(
            sorted_dates=all_dates,
            train_bars=self.train_bars,
            test_bars=self.test_bars,
            purge_bars=self.purge_bars,
        )

        fold_results: List[FoldResult] = []

        for window in windows:
            train_univ = self._slice_universe(universe_dfs, window.train_dates)
            test_univ = self._slice_universe(universe_dfs, window.test_dates)
            train_spy = self._slice_universe({"SPY": spy_df}, window.train_dates)["SPY"] if spy_df is not None else None
            test_spy = self._slice_universe({"SPY": spy_df}, window.test_dates)["SPY"] if spy_df is not None else None

            # --- STEP 1: In-Sample Optimization (Train ONLY) ---
            best_param = None
            best_train_metric = -float("inf")
            best_train_result: Optional[BacktestResult] = None

            for param in self.parameter_grid:
                # Configure backtester with candidate parameter
                bt = Tier1VectorizedBacktester(
                    initial_capital=100.0,
                    slot_target_usd=param.get("slot_target_usd", 30.0),
                    max_risk_cap_usd=param.get("max_risk_cap_usd", 3.0),
                    half_spread_bps=param.get("half_spread_bps", 3.0),
                )
                res = bt.run(train_univ, spy_df=train_spy)
                metric_val = getattr(res, self.metric_key, res.profit_factor)

                if metric_val > best_train_metric:
                    best_train_metric = metric_val
                    best_param = param
                    best_train_result = res

            if best_param is None:
                best_param = self.parameter_grid[0]
                best_train_result = Tier1VectorizedBacktester().run(train_univ, spy_df=train_spy)

            # --- STEP 2: Out-of-Sample Evaluation (Test ONLY) ---
            # Freezing selected best_param
            bt_test = Tier1VectorizedBacktester(
                initial_capital=100.0,
                slot_target_usd=best_param.get("slot_target_usd", 30.0),
                max_risk_cap_usd=best_param.get("max_risk_cap_usd", 3.0),
                half_spread_bps=best_param.get("half_spread_bps", 3.0),
            )
            test_res = bt_test.run(test_univ, spy_df=test_spy)
            regime = self._detect_regime(spy_df, window.test_dates)

            fold_results.append(
                FoldResult(
                    fold_idx=window.fold_idx,
                    train_period=(window.train_start, window.train_end),
                    test_period=(window.test_start, window.test_end),
                    best_parameter=best_param,
                    train_metrics={
                        "profit_factor": best_train_result.profit_factor if best_train_result else 0.0,
                        "sharpe": best_train_result.sharpe_ratio if best_train_result else 0.0,
                        "win_rate": best_train_result.win_rate if best_train_result else 0.0,
                        "total_trades": best_train_result.total_trades if best_train_result else 0,
                    },
                    test_metrics={
                        "profit_factor": test_res.profit_factor,
                        "sharpe": test_res.sharpe_ratio,
                        "win_rate": test_res.win_rate,
                        "total_trades": test_res.total_trades,
                    },
                    test_regime=regime,
                )
            )

        # Aggregate summaries
        train_pfs = [f.train_metrics["profit_factor"] for f in fold_results]
        test_pfs = [f.test_metrics["profit_factor"] for f in fold_results]
        train_sharpes = [f.train_metrics["sharpe"] for f in fold_results]
        test_sharpes = [f.test_metrics["sharpe"] for f in fold_results]

        avg_train_pf = float(np.mean(train_pfs)) if train_pfs else 0.0
        avg_test_pf = float(np.mean(test_pfs)) if test_pfs else 0.0
        avg_train_sharpe = float(np.mean(train_sharpes)) if train_sharpes else 0.0
        avg_test_sharpe = float(np.mean(test_sharpes)) if test_sharpes else 0.0

        deg_pct = (
            ((avg_train_pf - avg_test_pf) / avg_train_pf) * 100.0 if avg_train_pf > 0 else 0.0
        )

        # Aggregate by regime
        regimes: Dict[str, Dict[str, Any]] = {}
        for f in fold_results:
            r = f.test_regime
            if r not in regimes:
                regimes[r] = {"folds": 0, "test_pfs": [], "test_sharpes": []}
            regimes[r]["folds"] += 1
            regimes[r]["test_pfs"].append(f.test_metrics["profit_factor"])
            regimes[r]["test_sharpes"].append(f.test_metrics["sharpe"])

        regime_breakdown = {}
        for r, data in regimes.items():
            regime_breakdown[r] = {
                "folds_count": data["folds"],
                "avg_test_profit_factor": round(float(np.mean(data["test_pfs"])), 2),
                "avg_test_sharpe": round(float(np.mean(data["test_sharpes"])), 2),
            }

        active_manifest = manifest or DataManifest.from_universe(universe_dfs)

        return WalkForwardReport(
            total_folds=len(fold_results),
            parameter_grid=self.parameter_grid,
            fold_results=fold_results,
            in_sample_avg_profit_factor=round(avg_train_pf, 2),
            out_of_sample_avg_profit_factor=round(avg_test_pf, 2),
            in_sample_avg_sharpe=round(avg_train_sharpe, 2),
            out_of_sample_avg_sharpe=round(avg_test_sharpe, 2),
            performance_degradation_pct=round(deg_pct, 1),
            regime_breakdown=regime_breakdown,
            manifest_id=active_manifest.manifest_id,
            code_version=active_manifest.code_version,
        )
