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


class Tier1VectorizedBacktester:
    def __init__(
        self,
        initial_capital: float = 100.0,
        max_slots: int = 3,
        slot_target_usd: float = 30.0,
        cash_buffer_usd: float = 10.0,
        max_risk_cap_usd: float = 3.0,
        half_spread_bps: float = 3.0,
    ):
        self.initial_capital = initial_capital
        self.max_slots = max_slots
        self.slot_target_usd = slot_target_usd
        self.cash_buffer_usd = cash_buffer_usd
        self.max_risk_cap_usd = max_risk_cap_usd
        self.half_spread_pct = half_spread_bps / 10000.0
        self.screener = QuantitativeScreener(
            slot_target_usd=slot_target_usd, max_risk_cap_usd=max_risk_cap_usd
        )

    def run(
        self, universe_dfs: Dict[str, pd.DataFrame], spy_df: Optional[pd.DataFrame] = None
    ) -> BacktestResult:
        """
        Runs day-by-day vectorized bar progression.
        Guarantees: Signal[t-1] executes at Open[t].
        """
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

        for idx, date in enumerate(sorted_dates):
            if idx == 0:
                continue

            # 1. Execute PENDING SIGNALS from yesterday at today's OPEN
            for sig in pending_signals:
                sym = sig["ticker"]
                df_sym = enriched_universe.get(sym)
                if df_sym is not None and date in df_sym.index:
                    open_price = df_sym.loc[date, "open"]
                    # Apply half-spread slippage to buy entry
                    exec_price = open_price * (1.0 + self.half_spread_pct)

                    # Check slot availability & cash buffer
                    available_cash = max(0.0, cash - self.cash_buffer_usd)
                    if len(open_positions) < self.max_slots and available_cash >= 10.0:
                        risk_per_share = max(0.50, exec_price - sig["stop_loss"])
                        alloc_cap = min(
                            self.slot_target_usd,
                            available_cash,
                            (self.max_risk_cap_usd * exec_price) / risk_per_share,
                        )
                        qty = math.floor((alloc_cap / exec_price) * 10000) / 10000.0
                        if qty > 0:
                            cost = round(qty * exec_price, 2)
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

                # Check gap-through stop on open
                if open_p <= pos["stop_loss"]:
                    exit_price = open_p * (1.0 - self.half_spread_pct)
                    exit_reason = "GAP_STOP"
                elif low <= pos["stop_loss"]:
                    exit_price = pos["stop_loss"] * (1.0 - self.half_spread_pct)
                    exit_reason = "STOP_LOSS"
                elif high >= pos["take_profit"]:
                    exit_price = pos["take_profit"] * (1.0 - self.half_spread_pct)
                    exit_reason = "TAKE_PROFIT"
                elif pos["holding_days"] >= 10:  # Time stop
                    exit_price = close * (1.0 - self.half_spread_pct)
                    exit_reason = "TIME_STOP"

                if exit_price:
                    proceeds = round(pos["qty"] * exit_price, 2)
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
        )
