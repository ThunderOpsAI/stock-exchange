"""
Unit tests for Market Data Pipeline and Quantitative Screener.
Tests macro regime, liquidity pre-filters, dual-strategy pattern matching,
$3.00 risk cap sizing, and composite Z-score ranking.
"""

from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import pytest

from src.data.pipeline import MarketDataPipeline
from src.domain.models import StrategyType
from src.screener.screener import QuantitativeScreener


def generate_synthetic_bars(
    num_bars: int = 220,
    base_price: float = 100.0,
    trend_slope: float = 0.2,
    volatility: float = 1.0,
    volume_base: float = 1_000_000.0,
) -> pd.DataFrame:
    """Generates synthetic daily OHLCV dataframe."""
    np.random.seed(42)
    dates = [
        datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
        for i in range(num_bars)
    ]
    closes = []
    curr = base_price
    for i in range(num_bars):
        curr = curr + trend_slope + np.random.normal(0, volatility)
        closes.append(curr)

    closes = np.array(closes)
    highs = closes + np.random.uniform(0.5, 1.5, size=num_bars)
    lows = closes - np.random.uniform(0.5, 1.5, size=num_bars)
    opens = closes + np.random.normal(0, 0.5, size=num_bars)
    volumes = np.random.uniform(volume_base * 0.8, volume_base * 1.2, size=num_bars)

    df = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        },
        index=dates,
    )
    return df


def test_market_data_pipeline_indicators():
    df = generate_synthetic_bars(100)
    enriched = MarketDataPipeline.compute_indicators(df)

    assert "sma_50" in enriched.columns
    assert "sma_200" in enriched.columns
    assert "ema_20" in enriched.columns
    assert "atr_14" in enriched.columns
    assert "rsi_14" in enriched.columns
    assert "rvol_20" in enriched.columns
    assert "addv_20" in enriched.columns
    assert enriched["atr_14"].iloc[-1] > 0.0


def test_macro_regime_filter():
    screener = QuantitativeScreener()

    # Bullish SPY
    spy_df = generate_synthetic_bars(220, base_price=400.0, trend_slope=0.5)
    spy_enriched = MarketDataPipeline.compute_indicators(spy_df)
    passed, msg = screener.check_macro_regime(spy_enriched)
    assert passed is True
    assert "Bullish" in msg

    # Bearish SPY (close well below SMA 200)
    spy_bear = generate_synthetic_bars(220, base_price=500.0, trend_slope=-1.0)
    spy_bear_enriched = MarketDataPipeline.compute_indicators(spy_bear)
    passed_bear, msg_bear = screener.check_macro_regime(spy_bear_enriched)
    assert passed_bear is False
    assert "<= SMA 200" in msg_bear


def test_liquidity_prefilters():
    screener = QuantitativeScreener(min_addv=25_000_000.0, min_price=15.0)

    # 1. Valid high liquidity large cap
    df_valid = generate_synthetic_bars(50, base_price=150.0, volume_base=500_000.0)  # ADDV ~ $75M
    df_valid = MarketDataPipeline.compute_indicators(df_valid)
    ok, _ = screener.check_liquidity_prefilter(df_valid)
    assert ok is True

    # 2. Penny stock (< $15)
    df_penny = generate_synthetic_bars(50, base_price=10.0, volume_base=500_000.0)
    df_penny = MarketDataPipeline.compute_indicators(df_penny)
    ok_penny, msg_penny = screener.check_liquidity_prefilter(df_penny)
    assert ok_penny is False
    assert "Price" in msg_penny

    # 3. Illiquid stock (low ADDV)
    df_illiquid = generate_synthetic_bars(50, base_price=50.0, volume_base=10_000.0)  # ADDV ~ $500k
    df_illiquid = MarketDataPipeline.compute_indicators(df_illiquid)
    ok_illiq, msg_illiq = screener.check_liquidity_prefilter(df_illiquid)
    assert ok_illiq is False
    assert "ADDV20" in msg_illiq


def test_trend_pullback_detection_and_sizing():
    screener = QuantitativeScreener()

    # Construct uptrending stock
    df = generate_synthetic_bars(220, base_price=100.0, trend_slope=0.3, volume_base=1_000_000.0)
    df = MarketDataPipeline.compute_indicators(df)

    # Ensure last 3 bars simulate a 20 EMA pullback and reversal trigger
    ema20 = df["ema_20"].iloc[-1]
    df.iloc[-2, df.columns.get_loc("low")] = ema20 * 0.995  # touched EMA 20
    df.iloc[-2, df.columns.get_loc("high")] = ema20 * 1.02
    df.iloc[-2, df.columns.get_loc("close")] = ema20 * 1.00

    # Today's reversal bar: green, above prior high, high RVOL
    df.iloc[-1, df.columns.get_loc("open")] = ema20 * 1.00
    df.iloc[-1, df.columns.get_loc("low")] = ema20 * 0.998
    df.iloc[-1, df.columns.get_loc("high")] = ema20 * 1.05
    df.iloc[-1, df.columns.get_loc("close")] = ema20 * 1.04  # close > open & close > high_prev
    df.iloc[-1, df.columns.get_loc("volume")] = df["volume"].iloc[-2] * 2.0
    df.iloc[-1, df.columns.get_loc("rvol_20")] = 1.65
    df.iloc[-1, df.columns.get_loc("rs_spy_63d")] = 1.15

    candidate = screener.evaluate_trend_pullback("NVDA", df)
    assert candidate is not None
    assert candidate.ticker == "NVDA"
    assert candidate.strategy == StrategyType.TREND_PULLBACK
    assert candidate.allocated_usd <= 30.0
    # Risk cap check: Qty * Risk <= $3.00 (or close to $3.00)
    shares = candidate.allocated_usd / candidate.entry_est
    total_dollar_risk = shares * candidate.risk_r
    assert total_dollar_risk <= 3.05  # Within rounding tolerance


def test_mean_reversion_detection():
    screener = QuantitativeScreener()

    df = generate_synthetic_bars(220, base_price=120.0, trend_slope=0.1, volume_base=1_000_000.0)
    df = MarketDataPipeline.compute_indicators(df)

    # Force oversold setup
    df.iloc[-1, df.columns.get_loc("rsi_14")] = 28.0
    sma_200 = df["sma_200"].iloc[-1]
    curr_base = sma_200 + 10.0
    df.iloc[-2, df.columns.get_loc("high")] = curr_base - 0.5
    df.iloc[-1, df.columns.get_loc("low")] = curr_base - 5.0
    df.iloc[-1, df.columns.get_loc("high")] = curr_base + 1.0
    df.iloc[-1, df.columns.get_loc("open")] = curr_base - 1.0
    df.iloc[-1, df.columns.get_loc("close")] = curr_base + 0.5  # close - low = 5.5, high - low = 6.0 (91% wick)

    candidate = screener.evaluate_mean_reversion("GOOGL", df)
    assert candidate is not None
    assert candidate.ticker == "GOOGL"
    assert candidate.strategy == StrategyType.MEAN_REVERSION
    assert candidate.allocated_usd <= 30.0


def test_composite_ranking():
    screener = QuantitativeScreener()

    c1 = screener.evaluate_trend_pullback("AAPL", generate_synthetic_bars(100))
    # Synthetic candidates
    now = datetime.now(timezone.utc)
    from src.domain.models import CandidateStatus, ScreenedCandidate
    cand1 = ScreenedCandidate(
        candidate_id="c1",
        timestamp=now,
        ticker="AAPL",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=150.0,
        stop_loss=145.0,
        take_profit=160.0,
        risk_r=5.0,
        allocated_usd=30.0,
        rank_score=0.0,
        status=CandidateStatus.PENDING_DELIBERATION,
    )
    cand2 = ScreenedCandidate(
        candidate_id="c2",
        timestamp=now,
        ticker="MSFT",
        strategy=StrategyType.MEAN_REVERSION,
        entry_est=300.0,
        stop_loss=290.0,
        take_profit=315.0,
        risk_r=10.0,
        allocated_usd=30.0,
        rank_score=0.0,
        status=CandidateStatus.PENDING_DELIBERATION,
    )

    df_aapl = generate_synthetic_bars(50, base_price=150.0)
    df_aapl = MarketDataPipeline.compute_indicators(df_aapl)
    df_aapl["rs_spy_63d"] = 1.30
    df_aapl["rvol_20"] = 2.0

    df_msft = generate_synthetic_bars(50, base_price=300.0)
    df_msft = MarketDataPipeline.compute_indicators(df_msft)
    df_msft["rs_spy_63d"] = 0.95
    df_msft["rvol_20"] = 1.0

    universe = {"AAPL": df_aapl, "MSFT": df_msft}
    ranked = screener.rank_candidates([cand1, cand2], universe)
    assert len(ranked) == 2
    # AAPL has higher RS and RVOL and is Trend Pullback -> higher rank
    assert ranked[0].ticker == "AAPL"
