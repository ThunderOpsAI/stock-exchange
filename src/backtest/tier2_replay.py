"""
Tier 2: Event-Driven Historical Replay Engine.
Full-fidelity, bar-by-bar state machine simulation modeling:
- Fractional share rounding (4 decimals floored)
- Dynamic volatility spread multiplier: Spread_eff(t) = Spread_base * max(1.0, ATR14(t) / SMA63(ATR))
- Overnight gap-through stop loss execution
- SQLite WAL SHA-256 LLM deliberation caching (REPLAY_STRICT / RECORD_ON_MISS / SYNTHETIC_MOCK)
- Two-tier circuit breaker and bracket watchdog
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from src.broker.simulated import SimulatedPaperBroker
from src.data.pipeline import MarketDataPipeline
from src.domain.models import (
    CandidateStatus,
    ExitReason,
    PositionStatus,
    ScreenedCandidate,
    StrategyType,
    VerdictOutcome,
)
from src.llm.committee import CacheMode, TriadLLMCommittee
from src.risk.engine import RiskEngine
from src.screener.screener import QuantitativeScreener
from src.storage.db import Database


@dataclass
class ReplayTradeRecord:
    ticker: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    exit_reason: str
    holding_days: int


@dataclass
class ReplayReport:
    regime_name: str
    start_date: str
    end_date: str
    initial_equity: float
    final_equity: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    max_drawdown_pct: float
    soft_halts_triggered: int
    hard_liquidations_triggered: int
    trades: List[ReplayTradeRecord] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)


class Tier2HistoricalReplayEngine:
    def __init__(
        self,
        db: Database,
        cache_mode: CacheMode = CacheMode.RECORD_ON_MISS,
        initial_capital: float = 100.0,
        base_spread_bps: float = 3.0,
    ):
        self.db = db
        self.cache_mode = cache_mode
        self.initial_capital = initial_capital
        self.base_spread_bps = base_spread_bps

        self.screener = QuantitativeScreener()
        self.committee = TriadLLMCommittee(db=self.db, cache_mode=self.cache_mode)

    def _calc_dynamic_spread(self, df_sym: pd.DataFrame, current_idx: int) -> float:
        """
        Calculates dynamic spread multiplier:
        Spread_eff = base_spread * max(1.0, ATR14(t) / SMA63(ATR))
        """
        if current_idx < 14:
            return self.base_spread_bps / 10000.0

        atr14 = df_sym["atr_14"].iloc[current_idx]
        atr_window = df_sym["atr_14"].iloc[max(0, current_idx - 63) : current_idx + 1]
        mean_atr63 = atr_window.mean() if len(atr_window) > 0 else atr14

        multiplier = max(1.0, (atr14 / mean_atr63) if mean_atr63 > 1e-4 else 1.0)
        eff_bps = self.base_spread_bps * multiplier
        return eff_bps / 10000.0

    def run_replay(
        self,
        universe_dfs: Dict[str, pd.DataFrame],
        spy_df: Optional[pd.DataFrame] = None,
        regime_name: str = "Historical Stress Replay",
    ) -> ReplayReport:
        """Executes full event-driven bar-by-bar historical replay."""
        broker = SimulatedPaperBroker(initial_cash=self.initial_capital, slippage_bps=0.0)
        risk_engine = RiskEngine(db=self.db, broker=broker)

        # Precompute indicators
        enriched_universe = {}
        for sym, df in universe_dfs.items():
            enriched_universe[sym] = MarketDataPipeline.compute_indicators(df, spy_df=spy_df)

        spy_enriched = (
            MarketDataPipeline.compute_indicators(spy_df) if spy_df is not None else None
        )

        all_dates = sorted(list(set().union(*[df.index for df in universe_dfs.values()])))
        trade_records: List[ReplayTradeRecord] = []
        equity_curve: List[float] = [self.initial_capital]
        soft_halts = 0
        hard_liquidations = 0

        pending_orders_for_open: List[ScreenedCandidate] = []

        for bar_idx, date in enumerate(all_dates):
            date_str = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)

            # 1. Opening Execution of yesterday's approved candidates
            for cand in pending_orders_for_open:
                sym = cand.ticker
                df_sym = enriched_universe.get(sym)
                if df_sym is not None and date in df_sym.index:
                    open_price = float(df_sym.loc[date, "open"])
                    eff_spread = self._calc_dynamic_spread(
                        df_sym, df_sym.index.get_loc(date)
                    )
                    exec_price = open_price * (1.0 + (eff_spread / 2.0))

                    # Update simulated price feed
                    broker.set_price(sym, exec_price)
                    cand.entry_est = exec_price

                    ok, order, res, msg = risk_engine.validate_and_route_order(cand)

            pending_orders_for_open = []

            # 2. Check Intraday / Close Price Updates & Bracket Watchdog
            current_prices = {}
            for sym, df_sym in enriched_universe.items():
                if date in df_sym.index:
                    current_prices[sym] = float(df_sym.loc[date, "close"])
                    broker.set_price(sym, current_prices[sym])

            # Run watchdog with overnight gap-stop checking
            open_pos = broker.get_positions()
            for pos in open_pos:
                df_sym = enriched_universe.get(pos.ticker)
                if df_sym is not None and date in df_sym.index:
                    bar = df_sym.loc[date]
                    eff_spread = self._calc_dynamic_spread(
                        df_sym, df_sym.index.get_loc(date)
                    )
                    half_spread = eff_spread / 2.0

                    # Gap-through stop check on open
                    if bar["open"] <= pos.stop_loss:
                        gap_exit_price = bar["open"] * (1.0 - half_spread)
                        broker.set_price(pos.ticker, gap_exit_price)
                        broker.close_position(pos.position_id)
                        pnl = round((gap_exit_price - pos.entry_price) * pos.qty, 2)
                        trade_records.append(
                            ReplayTradeRecord(
                                ticker=pos.ticker,
                                entry_date=pos.opened_at.strftime("%Y-%m-%d"),
                                exit_date=date_str,
                                entry_price=pos.entry_price,
                                exit_price=gap_exit_price,
                                qty=pos.qty,
                                pnl=pnl,
                                exit_reason="GAP_STOP",
                                holding_days=1,
                            )
                        )
                    # Normal intraday low stop
                    elif bar["low"] <= pos.stop_loss:
                        exit_p = pos.stop_loss * (1.0 - half_spread)
                        broker.set_price(pos.ticker, exit_p)
                        broker.close_position(pos.position_id)
                        pnl = round((exit_p - pos.entry_price) * pos.qty, 2)
                        trade_records.append(
                            ReplayTradeRecord(
                                ticker=pos.ticker,
                                entry_date=pos.opened_at.strftime("%Y-%m-%d"),
                                exit_date=date_str,
                                entry_price=pos.entry_price,
                                exit_price=exit_p,
                                qty=pos.qty,
                                pnl=pnl,
                                exit_reason="STOP_LOSS",
                                holding_days=1,
                            )
                        )
                    # Take profit target
                    elif bar["high"] >= pos.take_profit:
                        tp_exit_p = pos.take_profit * (1.0 - half_spread)
                        broker.set_price(pos.ticker, tp_exit_p)
                        broker.close_position(pos.position_id)
                        pnl = round((tp_exit_p - pos.entry_price) * pos.qty, 2)
                        trade_records.append(
                            ReplayTradeRecord(
                                ticker=pos.ticker,
                                entry_date=pos.opened_at.strftime("%Y-%m-%d"),
                                exit_date=date_str,
                                entry_price=pos.entry_price,
                                exit_price=tp_exit_p,
                                qty=pos.qty,
                                pnl=pnl,
                                exit_reason="TAKE_PROFIT",
                                holding_days=1,
                            )
                        )

            # 3. Snapshot portfolio & verify circuit breakers
            snap = risk_engine.record_portfolio_snapshot()
            equity_curve.append(snap.total_equity)

            if snap.circuit_breaker_tier.value == 1:
                soft_halts += 1
            elif snap.circuit_breaker_tier.value == 2:
                hard_liquidations += 1

            # 4. Generate candidate setups on bar close for tomorrow's open
            if len(broker.get_positions()) < 3 and snap.total_equity > 80.0:
                current_slice = {}
                for sym, df_sym in enriched_universe.items():
                    if date in df_sym.index:
                        sub = df_sym.loc[:date]
                        if len(sub) >= 25:
                            current_slice[sym] = sub

                spy_sub = (
                    spy_enriched.loc[:date]
                    if (spy_enriched is not None and date in spy_enriched.index)
                    else None
                )
                avail_slots = 3 - len(broker.get_positions())
                candidates = self.screener.scan_universe(
                    current_slice, spy_df=spy_sub, top_n=avail_slots
                )

                for cand in candidates:
                    # Deliberate with LLM committee (cached)
                    verdict, _ = self.committee.deliberate(cand, df=current_slice.get(cand.ticker))
                    if verdict.verdict_outcome == VerdictOutcome.AUTO_APPROVED:
                        pending_orders_for_open.append(cand)

        # Summary statistics
        total_t = len(trade_records)
        wins = [t.pnl for t in trade_records if t.pnl > 0]
        losses = [t.pnl for t in trade_records if t.pnl <= 0]
        win_rate = len(wins) / total_t if total_t > 0 else 0.0
        gross_p = sum(wins) if wins else 0.0
        gross_l = abs(sum(losses)) if losses else 0.0
        pf = gross_p / gross_l if gross_l > 0 else (10.0 if gross_p > 0 else 0.0)

        eq_arr = np.array(equity_curve)
        peak = np.maximum.accumulate(eq_arr)
        drawdowns = (eq_arr - peak) / peak
        max_dd = abs(float(np.min(drawdowns))) * 100.0 if len(drawdowns) > 0 else 0.0

        return ReplayReport(
            regime_name=regime_name,
            start_date=all_dates[0].strftime("%Y-%m-%d") if all_dates else "",
            end_date=all_dates[-1].strftime("%Y-%m-%d") if all_dates else "",
            initial_equity=self.initial_capital,
            final_equity=equity_curve[-1],
            total_trades=total_t,
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=round(win_rate, 4),
            profit_factor=round(pf, 2),
            max_drawdown_pct=round(max_dd, 2),
            soft_halts_triggered=soft_halts,
            hard_liquidations_triggered=hard_liquidations,
            trades=trade_records,
            equity_curve=equity_curve,
        )
