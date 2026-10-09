"""
Tier 1: Macro Vectorized Backtesting Engine.
Executes fast multi-year simulation across daily OHLCV bars.
Strictly enforces:
- Anti-lookahead bias (Entry[t] = Signal[t-1], Open[t] execution)
- Max 3 active slots on $100 capital base
- Half-spread slippage
- Performance metrics: Expectancy (E in R), Profit Factor, Sharpe, Max Drawdown, Circuit Breaker Breaches.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from src.backtest.costs import CostModelConfig, ExecutionCostModel
from src.backtest.manifest import DataManifest
from src.data.pipeline import MarketDataPipeline
from src.screener.screener import QuantitativeScreener


@dataclass
class BacktestResult:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    expectancy_r: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    circuit_breaker_breaches: int
    final_equity: float
    equity_curve: List[float]
    manifest_id: Optional[str] = None
    manifest: Optional[DataManifest] = None
    code_version: Optional[Dict[str, str]] = None
    cost_summary: Optional[Dict[str, Any]] = None
    cost_assumptions: Optional[Dict[str, Any]] = None


class Tier1VectorizedBacktester:
    def __init__(
        self,
        initial_capital: float = 100.0,
        max_slots: int = 3,
        slot_target_usd: float = 30.0,
        cash_buffer_usd: float = 10.0,
        max_risk_cap_usd: float = 3.0,
        half_spread_bps: float = 3.0,
        cost_config: Optional[CostModelConfig] = None,
    ):
        self.initial_capital = initial_capital
        self.max_slots = max_slots
        self.slot_target_usd = slot_target_usd
        self.cash_buffer_usd = cash_buffer_usd
        self.max_risk_cap_usd = max_risk_cap_usd
        self.half_spread_pct = half_spread_bps / 10000.0
        self.cost_config = cost_config or CostModelConfig(
            half_spread_bps=half_spread_bps, slippage_bps=0.0
        )
        self.cost_model = ExecutionCostModel(self.cost_config)
        self.screener = QuantitativeScreener(
            slot_target_usd=slot_target_usd, max_risk_cap_usd=max_risk_cap_usd
        )

    def run(
        self,
        universe_dfs: Dict[str, pd.DataFrame],
        spy_df: Optional[pd.DataFrame] = None,
        manifest: Optional[DataManifest] = None,
    ) -> BacktestResult:
        """
        Runs day-by-day vectorized bar progression.
        Guarantees: Signal[t-1] executes at Open[t].
        Attaches reproducible DataManifest.
        """
        if manifest is not None:
            is_valid, errors = manifest.verify_data_integrity(universe_dfs)
            if not is_valid:
                raise ValueError(f"Data manifest integrity verification failed: {'; '.join(errors)}")
            active_manifest = manifest
        else:
            active_manifest = DataManifest.from_universe(
                universe_dfs=universe_dfs,
                assumptions={
                    "initial_capital_usd": self.initial_capital,
                    "max_slots": self.max_slots,
                    "slot_target_usd": self.slot_target_usd,
                    "cash_buffer_usd": self.cash_buffer_usd,
                    "max_risk_cap_usd": self.max_risk_cap_usd,
                    "half_spread_bps": self.half_spread_pct * 10000.0,
                },
            )

        # Align all dates across universe
        all_dates = set()
        for df in universe_dfs.values():
            all_dates.update(df.index)
        sorted_dates = sorted(list(all_dates))

        cash = self.initial_capital
        equity = self.initial_capital
        open_positions: List[Dict] = []  # active trades
        trade_pnls: List[float] = []
        trade_r_multiples: List[float] = []
        equity_curve: List[float] = [equity]
        circuit_breaker_breaches = 0

        # Precompute indicators for all symbols
        enriched_universe = {}
        for sym, df in universe_dfs.items():
            enriched_universe[sym] = MarketDataPipeline.compute_indicators(df, spy_df=spy_df)

        spy_enriched = (
            MarketDataPipeline.compute_indicators(spy_df) if spy_df is not None else None
        )

        pending_signals: List[Dict] = []

        self.cost_model.reset_summary()

        for idx, date in enumerate(sorted_dates):
            if idx == 0:
                continue

            # Process corporate actions on open positions for today's date
            for pos in open_positions:
                new_q, new_e, new_sl, new_tp, div_cash, delist = self.cost_model.process_corporate_actions_for_date(
                    ticker=pos["ticker"],
                    current_date_str=str(date),
                    position_shares=pos["qty"],
                    entry_price=pos["entry_price"],
                    stop_loss=pos["stop_loss"],
                    take_profit=pos["take_profit"],
                )
                pos["qty"] = new_q
                pos["entry_price"] = new_e
                pos["stop_loss"] = new_sl
                pos["take_profit"] = new_tp
                if div_cash > 0:
                    cash += div_cash
                if delist == "DELISTED":
                    pos["force_delist"] = True

            # 1. Execute PENDING SIGNALS from yesterday at today's OPEN
            for sig in pending_signals:
                sym = sig["ticker"]
                df_sym = enriched_universe.get(sym)
                if df_sym is not None and date in df_sym.index:
                    open_price = df_sym.loc[date, "open"]
                    bar_vol = df_sym.loc[date, "volume"] if "volume" in df_sym.columns else None

                    # Check slot availability & cash buffer
                    available_cash = max(0.0, cash - self.cash_buffer_usd)
                    if len(open_positions) < self.max_slots and available_cash >= 10.0:
                        risk_per_share = max(0.50, open_price - sig["stop_loss"])
                        alloc_cap = min(
                            self.slot_target_usd,
                            available_cash,
                            (self.max_risk_cap_usd * open_price) / risk_per_share,
                        )
                        target_shares = math.floor((alloc_cap / open_price) * 10000) / 10000.0
                        if target_shares > 0:
                            exec_price, qty, fee = self.cost_model.calculate_entry_execution(
                                raw_price=open_price,
                                target_shares=target_shares,
                                bar_volume=bar_vol,
                            )
                            if qty > 0:
                                cost = round(qty * exec_price, 2) + fee
                                cash -= cost
                                open_positions.append(
                                    {
                                        "ticker": sym,
                                        "qty": qty,
                                        "entry_price": exec_price,
                                        "stop_loss": sig["stop_loss"],
                                        "take_profit": sig["take_profit"],
                                        "entry_date": date,
                                        "holding_days": 0,
                                        "risk_r": risk_per_share,
                                    }
                                )

            pending_signals = []

            # 2. Check Exits for OPEN POSITIONS on today's bar
            remaining_positions = []
            for pos in open_positions:
                pos["holding_days"] += 1
                sym = pos["ticker"]
                df_sym = enriched_universe.get(sym)

                if df_sym is None or date not in df_sym.index:
                    remaining_positions.append(pos)
                    continue

                bar = df_sym.loc[date]
                high = bar["high"]
                low = bar["low"]
                close = bar["close"]
                open_p = bar["open"]

                exit_price = None
                exit_reason = None
                exit_fee = 0.0

                if pos.get("force_delist"):
                    exit_price, exit_fee, exit_reason = self.cost_model.calculate_exit_execution(
                        stop_price=open_p,
                        bar_open=open_p,
                        bar_low=low,
                        shares=pos["qty"],
                        is_stop_loss=True,
                    )
                    exit_reason = "DELISTED"
                elif open_p <= pos["stop_loss"] or low <= pos["stop_loss"]:
                    exit_price, exit_fee, exit_reason = self.cost_model.calculate_exit_execution(
                        stop_price=pos["stop_loss"],
                        bar_open=open_p,
                        bar_low=low,
                        shares=pos["qty"],
                        is_stop_loss=True,
                    )
                elif high >= pos["take_profit"]:
                    exit_price, exit_fee, exit_reason = self.cost_model.calculate_exit_execution(
                        stop_price=pos["take_profit"],
                        bar_open=open_p,
                        bar_low=low,
                        shares=pos["qty"],
                        is_stop_loss=False,
                    )
                    exit_reason = "TAKE_PROFIT"
                elif pos["holding_days"] >= 10:  # Time stop
                    exit_price, exit_fee, exit_reason = self.cost_model.calculate_exit_execution(
                        stop_price=close,
                        bar_open=open_p,
                        bar_low=low,
                        shares=pos["qty"],
                        is_stop_loss=False,
                    )
                    exit_reason = "TIME_STOP"

                if exit_price:
                    proceeds = round(pos["qty"] * exit_price, 2) - exit_fee
                    pnl = round(proceeds - (pos["qty"] * pos["entry_price"]), 2)
                    cash += proceeds
                    trade_pnls.append(pnl)
                    r_mult = pnl / (pos["qty"] * pos["risk_r"]) if pos["risk_r"] > 0 else 0.0
                    trade_r_multiples.append(r_mult)
                else:
                    remaining_positions.append(pos)

            open_positions = remaining_positions

            # 3. Calculate current mark-to-market equity
            pos_mkt_val = 0.0
            for pos in open_positions:
                df_sym = enriched_universe.get(pos["ticker"])
                if df_sym is not None and date in df_sym.index:
                    pos_mkt_val += pos["qty"] * df_sym.loc[date, "close"]
                else:
                    pos_mkt_val += pos["qty"] * pos["entry_price"]

            equity = round(cash + pos_mkt_val, 2)
            equity_curve.append(equity)

            # Check circuit breaker
            if equity <= 80.0:
                circuit_breaker_breaches += 1

            # 4. Generate candidate signals for TOMORROW's open (Signal[t])
            # Only generate if slots are available and equity > 80 (not soft halted)
            if len(open_positions) < self.max_slots and equity > 80.0:
                current_slice = {}
                for sym, df_sym in enriched_universe.items():
                    if date in df_sym.index:
                        sub = df_sym.loc[:date]
                        if len(sub) >= 25:
                            current_slice[sym] = sub

                spy_sub = None
                if spy_enriched is not None and date in spy_enriched.index:
                    spy_sub = spy_enriched.loc[:date]

                available_slots = self.max_slots - len(open_positions)
                candidates = self.screener.scan_universe(
                    current_slice, spy_df=spy_sub, top_n=available_slots
                )

                for cand in candidates:
                    # Avoid duplicate ticker
                    if not any(p["ticker"] == cand.ticker for p in open_positions):
                        pending_signals.append(
                            {
                                "ticker": cand.ticker,
                                "stop_loss": cand.stop_loss,
                                "take_profit": cand.take_profit,
                            }
                        )

        # Compute summary metrics
        total_trades = len(trade_pnls)
        wins = [p for p in trade_pnls if p > 0]
        losses = [p for p in trade_pnls if p <= 0]
        win_rate = len(wins) / total_trades if total_trades > 0 else 0.0
        gross_profit = sum(wins) if wins else 0.0
        gross_loss = abs(sum(losses)) if losses else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (10.0 if gross_profit > 0 else 0.0)
        expectancy_r = np.mean(trade_r_multiples) if trade_r_multiples else 0.0

        # Drawdown calculation
        eq_arr = np.array(equity_curve)
        peak = np.maximum.accumulate(eq_arr)
        drawdowns = (eq_arr - peak) / peak
        max_dd_pct = abs(float(np.min(drawdowns))) * 100.0 if len(drawdowns) > 0 else 0.0

        # Sharpe & Sortino (daily returns)
        daily_returns = pd.Series(equity_curve).pct_change().dropna()
        if len(daily_returns) > 5 and daily_returns.std() > 1e-6:
            sharpe = float((daily_returns.mean() / daily_returns.std()) * np.sqrt(252))
            downside_std = daily_returns[daily_returns < 0].std()
            sortino = (
                float((daily_returns.mean() / downside_std) * np.sqrt(252))
                if downside_std > 1e-6
                else sharpe
            )
        else:
            sharpe, sortino = 0.0, 0.0

        return BacktestResult(
            total_trades=total_trades,
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=round(win_rate, 4),
            profit_factor=round(profit_factor, 2),
            expectancy_r=round(expectancy_r, 2),
            sharpe_ratio=round(sharpe, 2),
            sortino_ratio=round(sortino, 2),
            max_drawdown_pct=round(max_dd_pct, 2),
            circuit_breaker_breaches=circuit_breaker_breaches,
            final_equity=equity,
            equity_curve=equity_curve,
            manifest_id=active_manifest.manifest_id,
            manifest=active_manifest,
            code_version=active_manifest.code_version,
            cost_summary=self.cost_model.summary.to_dict(),
            cost_assumptions=self.cost_model.config.to_dict(),
        )
