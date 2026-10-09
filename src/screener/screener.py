"""
Quantitative Swing Screener.
Implements dual-strategy quantitative setups for $100 capital sandbox:
- Strategy Alpha (60% weight): Trend-Leader 20 EMA Pullback
- Strategy Beta (40% weight): 14D RSI Oversold Dip
- Macro SPY regime check
- Liquidity pre-filters (ADDV20 >= $25M, spread <= 6 bps, price >= $15.00)
- Composite Z-Score ranking
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from src.domain.models import (
    CandidateStatus,
    ExecutionQuote,
    ScreenedCandidate,
    StrategyType,
)


class QuantitativeScreener:
    def __init__(
        self,
        min_addv: float = 25_000_000.0,
        max_spread_rel: float = 0.0006,  # 6 bps
        min_price: float = 15.0,
        slot_target_usd: float = 30.0,
        max_risk_cap_usd: float = 3.0,
        require_quote: bool = False,
        max_quote_age_seconds: float = 60.0,
        calendar_gate: Optional[Any] = None,
    ):
        self.min_addv = min_addv
        self.max_spread_rel = max_spread_rel
        self.min_price = min_price
        self.slot_target_usd = slot_target_usd
        self.max_risk_cap_usd = max_risk_cap_usd
        self.require_quote = require_quote
        self.max_quote_age_seconds = max_quote_age_seconds
        self.calendar_gate = calendar_gate

    def check_macro_regime(self, spy_df: Optional[pd.DataFrame]) -> Tuple[bool, str]:
        """
        Validates macro bull regime on SPY:
        Close_SPY > SMA_200 and SMA_50(SPY, t) >= SMA_50(SPY, t-5)
        Fails closed adhering to ADR 0002.
        """
        if spy_df is None or spy_df.empty or len(spy_df) < 200:
            return (
                False,
                "FAIL_CLOSED: Insufficient SPY history (requires at least 200 bars for SMA200 and SMA50 slope)",
            )

        latest = spy_df.iloc[-1]
        close = latest.get("close")
        sma_200 = latest.get("sma_200")
        sma_50 = latest.get("sma_50")
        sma_50_prev = spy_df.iloc[-6].get("sma_50") if len(spy_df) >= 6 else None

        if (
            close is None
            or pd.isna(close)
            or sma_200 is None
            or pd.isna(sma_200)
            or sma_50 is None
            or pd.isna(sma_50)
            or sma_50_prev is None
            or pd.isna(sma_50_prev)
        ):
            return (
                False,
                "FAIL_CLOSED: Required SPY indicators (SMA200/SMA50) missing or uncomputed",
            )

        close_val = float(close)
        sma_200_val = float(sma_200)
        sma_50_val = float(sma_50)
        sma_50_prev_val = float(sma_50_prev)

        if close_val <= sma_200_val:
            return False, f"SPY close (${close_val:.2f}) <= SMA 200 (${sma_200_val:.2f})"

        if sma_50_val < sma_50_prev_val:
            return (
                False,
                f"SPY SMA 50 deteriorating: current (${sma_50_val:.2f}) < 5-day prior (${sma_50_prev_val:.2f})",
            )

        return True, "SPY macro regime is Bullish (Close > SMA 200 and SMA 50 ascending)"

    def check_liquidity_prefilter(
        self,
        df: pd.DataFrame,
        quote: Optional[ExecutionQuote] = None,
        require_quote: Optional[bool] = None,
    ) -> Tuple[bool, str]:
        """
        Hard 3-layer liquidity pre-filter:
        1. ADDV20 >= $25M
        2. Spread_rel <= 6 bps (using real ExecutionQuote when provided)
        3. Price >= $15.00
        """
        should_require_quote = self.require_quote if require_quote is None else require_quote

        if df.empty or len(df) < 20:
            return False, "Insufficient bar history"

        latest = df.iloc[-1]
        price = latest["close"]
        if price < self.min_price:
            return False, f"Price ${price:.2f} < ${self.min_price:.2f}"

        addv_20 = latest.get("addv_20", 0.0)
        if pd.notna(addv_20) and addv_20 < self.min_addv:
            return False, f"ADDV20 ${addv_20:,.0f} < ${self.min_addv:,.0f}"

        if should_require_quote and quote is None:
            return False, "Missing execution quote: quote is required for execution decision"

        if quote is not None:
            if not quote.is_valid:
                return False, f"Invalid execution quote: bid=${quote.bid:.2f}, ask=${quote.ask:.2f}"

            now_utc = datetime.now(timezone.utc)
            quote_ts = quote.timestamp if quote.timestamp.tzinfo else quote.timestamp.replace(tzinfo=timezone.utc)
            age = (now_utc - quote_ts).total_seconds()
            if age > self.max_quote_age_seconds:
                return False, f"Stale execution quote: age {age:.1f}s > {self.max_quote_age_seconds:.1f}s"

            if quote.ask < self.min_price:
                return False, f"Quote ask price ${quote.ask:.2f} < ${self.min_price:.2f}"

            spread_rel = quote.spread_rel
            if spread_rel > self.max_spread_rel:
                return False, f"Spread {quote.spread_bps:.1f} bps > {self.max_spread_rel * 10000:.1f} bps"
        else:
            spread_rel = latest.get("spread_rel", 0.0003)
            if pd.notna(spread_rel) and spread_rel > self.max_spread_rel:
                return False, f"Spread {spread_rel * 10000:.1f} bps > {self.max_spread_rel * 10000:.1f} bps"

        return True, "Passed liquidity pre-filters"

    def evaluate_trend_pullback(
        self,
        ticker: str,
        df: pd.DataFrame,
        quote: Optional[ExecutionQuote] = None,
        require_quote: Optional[bool] = None,
    ) -> Optional[ScreenedCandidate]:
        """
        Strategy Alpha: Trend-Leader 20 EMA Pullback (60% weight).
        - Stock trend: Close > SMA200 and SMA50 > SMA200
        - Leader: RS_SPY_63d >= 1.05
        - Pullback: min(Low[t-2..t]) <= EMA20 * 1.01 and Low_t > SMA50
        - Reversal Trigger: Close_t > Open_t and Close_t > High_{t-1} and RVOL20 >= 1.20
        """
        should_require_quote = self.require_quote if require_quote is None else require_quote
        if should_require_quote and (quote is None or not quote.is_valid):
            return None

        if quote is not None and not quote.is_valid:
            return None

        if len(df) < 25:
            return None

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        close = curr["close"]
        open_p = curr["open"]
        high_prev = prev["high"]
        sma_200 = curr.get("sma_200")
        sma_50 = curr.get("sma_50")
        ema_20 = curr.get("ema_20")
        atr_14 = curr.get("atr_14", close * 0.02)
        rs_spy = curr.get("rs_spy_63d", 1.0)
        rvol = curr.get("rvol_20", 1.0)

        # 1. Trend alignment
        if pd.isna(sma_200) or pd.isna(sma_50) or pd.isna(ema_20):
            return None
        if not (close > sma_200 and sma_50 > sma_200):
            return None

        # 2. Leader relative strength
        if rs_spy < 1.05:
            return None

        # 3. Pullback condition
        recent_lows = df["low"].iloc[-3:].min()
        if not (recent_lows <= ema_20 * 1.01 and curr["low"] > sma_50):
            return None

        # 4. Reversal trigger: Green candle closing above prior high with RVOL >= 1.20
        if not (close > open_p and close > high_prev and rvol >= 1.20):
            return None

        # Entry pricing: derived from ask price for buys when quote is present
        entry_price = quote.ask if (quote is not None and quote.is_valid) else close

        # Stop loss: min(Low_{t-1}, Low_t) - 0.5 * ATR14
        stop_loss = round(min(prev["low"], curr["low"]) - 0.5 * atr_14, 2)
        if stop_loss >= entry_price:
            stop_loss = round(entry_price * 0.95, 2)

        risk_r = round(entry_price - stop_loss, 2)
        if risk_r <= 0.05:
            return None

        take_profit = round(entry_price + 2.0 * risk_r, 2)

        # Sizing with $3.00 max risk cap:
        # Allocated = min($30.00, ($3.00 * Entry) / (Entry - SL))
        max_capital_by_risk = (self.max_risk_cap_usd * entry_price) / risk_r
        allocated_usd = round(min(self.slot_target_usd, max_capital_by_risk), 2)

        # Raw score for ranking
        raw_score = (rs_spy * 0.5) + (rvol * 0.3)

        return ScreenedCandidate(
            candidate_id=f"cand_{ticker.lower()}_{uuid.uuid4().hex[:6]}",
            timestamp=datetime.now(timezone.utc),
            ticker=ticker.upper(),
            strategy=StrategyType.TREND_PULLBACK,
            entry_est=round(entry_price, 2),
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_r=risk_r,
            allocated_usd=allocated_usd,
            rank_score=round(raw_score, 4),
            status=CandidateStatus.PENDING_DELIBERATION,
        )

    def evaluate_mean_reversion(
        self,
        ticker: str,
        df: pd.DataFrame,
        quote: Optional[ExecutionQuote] = None,
        require_quote: Optional[bool] = None,
    ) -> Optional[ScreenedCandidate]:
        """
        Strategy Beta: 14D RSI Oversold Dip (40% weight).
        - Stock trend: Close > SMA200
        - Oversold: RSI14 <= 34 or Low_t <= EMA20 - 1.8 * ATR14
        - Reversal: Hammer candle (Close - Low) >= 0.6 * (High - Low) and Close > High_{t-1}
        """
        should_require_quote = self.require_quote if require_quote is None else require_quote
        if should_require_quote and (quote is None or not quote.is_valid):
            return None

        if quote is not None and not quote.is_valid:
            return None

        if len(df) < 25:
            return None

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        close = curr["close"]
        high = curr["high"]
        low = curr["low"]
        sma_200 = curr.get("sma_200")
        ema_20 = curr.get("ema_20")
        rsi_14 = curr.get("rsi_14", 50.0)
        atr_14 = curr.get("atr_14", close * 0.02)
        rvol = curr.get("rvol_20", 1.0)
        rs_spy = curr.get("rs_spy_63d", 1.0)

        # 1. Trend alignment
        if pd.isna(sma_200) or pd.isna(ema_20):
            return None
        if close <= sma_200:
            return None

        # 2. Oversold setup: RSI <= 34 OR Keltner channel touch
        keltner_lower = ema_20 - (1.8 * atr_14)
        is_oversold = (rsi_14 <= 34.0) or (low <= keltner_lower)
        if not is_oversold:
            return None

        # 3. Reversal candle: Hammer / bottom rejection & break above prior high
        candle_range = high - low
        lower_wick_reversal = False
        if candle_range > 0:
            lower_wick_reversal = (close - low) >= (0.60 * candle_range)

        if not (lower_wick_reversal and close > prev["high"]):
            return None

        # Entry pricing: derived from ask price for buys when quote is present
        entry_price = quote.ask if (quote is not None and quote.is_valid) else close

        # Stop loss: Low_t - 1.0 * ATR14
        stop_loss = round(low - 1.0 * atr_14, 2)
        if stop_loss >= entry_price:
            stop_loss = round(entry_price * 0.95, 2)

        risk_r = round(entry_price - stop_loss, 2)
        if risk_r <= 0.05:
            return None

        take_profit = round(entry_price + 1.5 * risk_r, 2)

        max_capital_by_risk = (self.max_risk_cap_usd * entry_price) / risk_r
        allocated_usd = round(min(self.slot_target_usd, max_capital_by_risk), 2)

        raw_score = (100.0 - rsi_14) / 50.0 + (rvol * 0.2)

        return ScreenedCandidate(
            candidate_id=f"cand_{ticker.lower()}_{uuid.uuid4().hex[:6]}",
            timestamp=datetime.now(timezone.utc),
            ticker=ticker.upper(),
            strategy=StrategyType.MEAN_REVERSION,
            entry_est=round(entry_price, 2),
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_r=risk_r,
            allocated_usd=allocated_usd,
            rank_score=round(raw_score, 4),
            status=CandidateStatus.PENDING_DELIBERATION,
        )

    def rank_candidates(
        self,
        candidates: List[ScreenedCandidate],
        universe_dfs: Dict[str, pd.DataFrame],
        quotes: Optional[Dict[str, ExecutionQuote]] = None,
    ) -> List[ScreenedCandidate]:
        """
        Composite Z-score ranking across detected setups:
        Rank = 0.40 * Z(RS) + 0.30 * Z(RVOL) + 0.15 * Z(ATR%) - 0.15 * Z(Spread)
        """
        if not candidates:
            return []
        if len(candidates) == 1:
            return candidates

        # Gather metrics for each candidate
        metrics = []
        for c in candidates:
            df = universe_dfs.get(c.ticker)
            quote = quotes.get(c.ticker) if quotes else None
            if quote is not None and quote.is_valid:
                spread = quote.spread_rel
            elif df is not None and not df.empty:
                latest = df.iloc[-1]
                spread = float(latest.get("spread_rel", 0.0003))
            else:
                spread = 0.0003

            if df is not None and not df.empty:
                latest = df.iloc[-1]
                rs = float(latest.get("rs_spy_63d", 1.0))
                rvol = float(latest.get("rvol_20", 1.0))
                atr_pct = float(latest.get("atr_pct", 2.0))
            else:
                rs, rvol, atr_pct = 1.0, 1.0, 2.0

            metrics.append({"cand": c, "rs": rs, "rvol": rvol, "atr_pct": atr_pct, "spread": spread})

        # Calculate Z-scores
        rs_vals = np.array([m["rs"] for m in metrics])
        rvol_vals = np.array([m["rvol"] for m in metrics])
        atr_vals = np.array([m["atr_pct"] for m in metrics])
        spr_vals = np.array([m["spread"] for m in metrics])

        def z_score(arr: np.ndarray) -> np.ndarray:
            std = np.std(arr)
            return (arr - np.mean(arr)) / std if std > 1e-6 else np.zeros_like(arr)

        z_rs = z_score(rs_vals)
        z_rvol = z_score(rvol_vals)
        z_atr = z_score(atr_vals)
        z_spr = z_score(spr_vals)

        ranked_list = []
        for i, m in enumerate(metrics):
            composite = 0.40 * z_rs[i] + 0.30 * z_rvol[i] + 0.15 * z_atr[i] - 0.15 * z_spr[i]
            # Strategy alpha bias (60% vs 40%)
            if m["cand"].strategy == StrategyType.TREND_PULLBACK:
                composite += 0.20

            cand = m["cand"]
            cand.rank_score = round(float(composite), 4)
            ranked_list.append(cand)

        ranked_list.sort(key=lambda x: x.rank_score, reverse=True)
        return ranked_list

    def scan_universe(
        self,
        universe_dfs: Dict[str, pd.DataFrame],
        spy_df: Optional[pd.DataFrame] = None,
        quotes: Optional[Dict[str, ExecutionQuote]] = None,
        top_n: int = 5,
        require_quote: Optional[bool] = None,
        calendar_gate: Optional[Any] = None,
        as_of: Optional[datetime] = None,
    ) -> List[ScreenedCandidate]:
        """Executes full scan over universe dataframes."""
        if spy_df is None and "SPY" in universe_dfs:
            spy_df = universe_dfs["SPY"]

        if spy_df is not None:
            regime_ok, _ = self.check_macro_regime(spy_df)
            if not regime_ok:
                return []

        active_gate = calendar_gate or self.calendar_gate

        candidates = []
        for ticker, df in universe_dfs.items():
            if ticker == "SPY":
                continue

            # Evaluate market calendar, session status, halt, and corporate action gate
            if active_gate is not None:
                gate_res = active_gate.evaluate_entry_gate(ticker, as_of=as_of)
                if not gate_res.passed:
                    continue

            quote = quotes.get(ticker) if quotes else None

            passed_liq, _ = self.check_liquidity_prefilter(
                df, quote=quote, require_quote=require_quote
            )
            if not passed_liq:
                continue

            # Check Trend Pullback
            c_trend = self.evaluate_trend_pullback(
                ticker, df, quote=quote, require_quote=require_quote
            )
            if c_trend:
                candidates.append(c_trend)
                continue  # A ticker takes one primary setup

            # Check Mean Reversion
            c_mean = self.evaluate_mean_reversion(
                ticker, df, quote=quote, require_quote=require_quote
            )
            if c_mean:
                candidates.append(c_mean)

        ranked = self.rank_candidates(candidates, universe_dfs, quotes=quotes)
        return ranked[:top_n]
