"""
Unit tests for Market Data Pipeline and Quantitative Screener.
Tests macro regime, liquidity pre-filters, dual-strategy pattern matching,
$3.00 risk cap sizing, and composite Z-score ranking.
"""

from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import pytest

pytestmark = pytest.mark.unit

from src.data.pipeline import MarketDataPipeline
from src.domain.models import ExecutionQuote, StrategyType
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
    assert msg == "SPY macro regime is Bullish (Close > SMA 200 and SMA 50 ascending)"

    # Bearish SPY (close well below SMA 200)
    spy_bear = generate_synthetic_bars(220, base_price=500.0, trend_slope=-1.0)
    spy_bear_enriched = MarketDataPipeline.compute_indicators(spy_bear)
    passed_bear, msg_bear = screener.check_macro_regime(spy_bear_enriched)
    assert passed_bear is False
    assert "<= SMA 200" in msg_bear


def test_macro_regime_none_empty_insufficient_history():
    screener = QuantitativeScreener()

    # 1. None
    passed, msg = screener.check_macro_regime(None)
    assert passed is False
    assert msg == "FAIL_CLOSED: Insufficient SPY history (requires at least 200 bars for SMA200 and SMA50 slope)"

    # 2. Empty DataFrame
    passed, msg = screener.check_macro_regime(pd.DataFrame())
    assert passed is False
    assert msg == "FAIL_CLOSED: Insufficient SPY history (requires at least 200 bars for SMA200 and SMA50 slope)"

    # 3. Insufficient history (< 200 bars, e.g. 50 bars)
    df_short = MarketDataPipeline.compute_indicators(generate_synthetic_bars(50))
    passed, msg = screener.check_macro_regime(df_short)
    assert passed is False
    assert msg == "FAIL_CLOSED: Insufficient SPY history (requires at least 200 bars for SMA200 and SMA50 slope)"

    # 4. Borderline insufficient (199 bars)
    df_199 = MarketDataPipeline.compute_indicators(generate_synthetic_bars(199))
    passed, msg = screener.check_macro_regime(df_199)
    assert passed is False
    assert msg == "FAIL_CLOSED: Insufficient SPY history (requires at least 200 bars for SMA200 and SMA50 slope)"


def test_macro_regime_nan_indicators():
    screener = QuantitativeScreener()

    # 1. Missing columns (raw bars without indicators)
    raw_df = generate_synthetic_bars(220)
    passed, msg = screener.check_macro_regime(raw_df)
    assert passed is False
    assert msg == "FAIL_CLOSED: Required SPY indicators (SMA200/SMA50) missing or uncomputed"

    # 2. NaN in sma_200 at latest bar
    spy_df = generate_synthetic_bars(220, base_price=400.0, trend_slope=0.5)
    spy_enriched = MarketDataPipeline.compute_indicators(spy_df)

    df_nan_200 = spy_enriched.copy()
    df_nan_200.iloc[-1, df_nan_200.columns.get_loc("sma_200")] = np.nan
    passed, msg = screener.check_macro_regime(df_nan_200)
    assert passed is False
    assert msg == "FAIL_CLOSED: Required SPY indicators (SMA200/SMA50) missing or uncomputed"

    # 3. NaN in sma_50 at latest bar
    df_nan_50 = spy_enriched.copy()
    df_nan_50.iloc[-1, df_nan_50.columns.get_loc("sma_50")] = np.nan
    passed, msg = screener.check_macro_regime(df_nan_50)
    assert passed is False
    assert msg == "FAIL_CLOSED: Required SPY indicators (SMA200/SMA50) missing or uncomputed"

    # 4. NaN in sma_50 at 5-day prior bar (t-5)
    df_nan_prev = spy_enriched.copy()
    df_nan_prev.iloc[-6, df_nan_prev.columns.get_loc("sma_50")] = np.nan
    passed, msg = screener.check_macro_regime(df_nan_prev)
    assert passed is False
    assert msg == "FAIL_CLOSED: Required SPY indicators (SMA200/SMA50) missing or uncomputed"


def test_macro_regime_close_below_sma_200():
    screener = QuantitativeScreener()

    # Bullish trend where we force close <= sma_200
    spy_df = generate_synthetic_bars(220, base_price=400.0, trend_slope=0.5)
    spy_enriched = MarketDataPipeline.compute_indicators(spy_df)

    df_below = spy_enriched.copy()
    sma_200 = df_below["sma_200"].iloc[-1]
    df_below.iloc[-1, df_below.columns.get_loc("close")] = sma_200 - 1.50
    close = df_below["close"].iloc[-1]

    passed, msg = screener.check_macro_regime(df_below)
    assert passed is False
    assert msg == f"SPY close (${close:.2f}) <= SMA 200 (${sma_200:.2f})"

    # Exactly equal: close == sma_200
    df_equal = spy_enriched.copy()
    df_equal.iloc[-1, df_equal.columns.get_loc("close")] = sma_200
    passed_eq, msg_eq = screener.check_macro_regime(df_equal)
    assert passed_eq is False
    assert msg_eq == f"SPY close (${sma_200:.2f}) <= SMA 200 (${sma_200:.2f})"


def test_macro_regime_sma_50_deterioration():
    screener = QuantitativeScreener()

    # Bullish SPY with close > sma_200
    spy_df = generate_synthetic_bars(220, base_price=400.0, trend_slope=0.5)
    spy_enriched = MarketDataPipeline.compute_indicators(spy_df)

    # Force SMA 50 at t < SMA 50 at t-5
    df_det = spy_enriched.copy()
    sma_50_prev = df_det["sma_50"].iloc[-6]
    sma_50_curr = sma_50_prev - 0.75
    df_det.iloc[-1, df_det.columns.get_loc("sma_50")] = sma_50_curr

    passed, msg = screener.check_macro_regime(df_det)
    assert passed is False
    assert msg == f"SPY SMA 50 deteriorating: current (${sma_50_curr:.2f}) < 5-day prior (${sma_50_prev:.2f})"


def test_macro_regime_full_valid_bull_regime():
    screener = QuantitativeScreener()

    # 1. Strictly ascending SMA 50 (SMA 50 > SMA 50 t-5) and Close > SMA 200
    spy_df = generate_synthetic_bars(220, base_price=400.0, trend_slope=0.5)
    spy_enriched = MarketDataPipeline.compute_indicators(spy_df)
    passed, msg = screener.check_macro_regime(spy_enriched)
    assert passed is True
    assert msg == "SPY macro regime is Bullish (Close > SMA 200 and SMA 50 ascending)"

    # 2. Equal SMA 50 (SMA 50 == SMA 50 t-5, not deteriorating)
    df_flat = spy_enriched.copy()
    df_flat.iloc[-1, df_flat.columns.get_loc("sma_50")] = df_flat["sma_50"].iloc[-6]
    passed_flat, msg_flat = screener.check_macro_regime(df_flat)
    assert passed_flat is True
    assert msg_flat == "SPY macro regime is Bullish (Close > SMA 200 and SMA 50 ascending)"


def test_scan_universe_blocked_when_macro_regime_unhealthy():
    """Verify scan_universe immediately aborts and returns an empty list when macro regime fails."""
    screener = QuantitativeScreener()
    now = datetime.now(timezone.utc)

    # Valid candidate ticker setup (NVDA)
    df_nvda = generate_synthetic_bars(220, base_price=100.0, trend_slope=0.3, volume_base=1_000_000.0)
    df_nvda = MarketDataPipeline.compute_indicators(df_nvda)
    ema20 = df_nvda["ema_20"].iloc[-1]
    df_nvda.iloc[-2, df_nvda.columns.get_loc("low")] = ema20 * 0.995
    df_nvda.iloc[-2, df_nvda.columns.get_loc("high")] = ema20 * 1.02
    df_nvda.iloc[-2, df_nvda.columns.get_loc("close")] = ema20 * 1.00
    df_nvda.iloc[-1, df_nvda.columns.get_loc("open")] = ema20 * 1.00
    df_nvda.iloc[-1, df_nvda.columns.get_loc("low")] = ema20 * 0.998
    df_nvda.iloc[-1, df_nvda.columns.get_loc("high")] = ema20 * 1.05
    df_nvda.iloc[-1, df_nvda.columns.get_loc("close")] = ema20 * 1.04
    df_nvda.iloc[-1, df_nvda.columns.get_loc("volume")] = df_nvda["volume"].iloc[-2] * 2.0
    df_nvda.iloc[-1, df_nvda.columns.get_loc("rvol_20")] = 1.65
    df_nvda.iloc[-1, df_nvda.columns.get_loc("rs_spy_63d")] = 1.15

    q_tight = ExecutionQuote(ticker="NVDA", bid=100.00, ask=100.02, timestamp=now)
    universe = {"NVDA": df_nvda}
    quotes = {"NVDA": q_tight}

    # 1. Bearish SPY (close <= SMA 200) -> blocked
    spy_bear = generate_synthetic_bars(220, base_price=500.0, trend_slope=-1.0)
    spy_bear_enriched = MarketDataPipeline.compute_indicators(spy_bear)
    candidates_bear = screener.scan_universe(universe, spy_df=spy_bear_enriched, quotes=quotes)
    assert candidates_bear == []

    # 2. Deteriorating SMA 50 -> blocked
    spy_bull = generate_synthetic_bars(220, base_price=400.0, trend_slope=0.5)
    spy_bull_enriched = MarketDataPipeline.compute_indicators(spy_bull)
    spy_det = spy_bull_enriched.copy()
    spy_det.iloc[-1, spy_det.columns.get_loc("sma_50")] = spy_det["sma_50"].iloc[-6] - 1.0
    candidates_det = screener.scan_universe(universe, spy_df=spy_det, quotes=quotes)
    assert candidates_det == []

    # 3. Insufficient history (< 200 bars) -> blocked
    spy_short = MarketDataPipeline.compute_indicators(generate_synthetic_bars(100))
    candidates_short = screener.scan_universe(universe, spy_df=spy_short, quotes=quotes)
    assert candidates_short == []

    # 4. Uncomputed indicators -> blocked
    spy_raw = generate_synthetic_bars(220)
    candidates_raw = screener.scan_universe(universe, spy_df=spy_raw, quotes=quotes)
    assert candidates_raw == []

    # 5. Passed in universe_dfs as SPY -> blocked if unhealthy
    universe_with_bear_spy = {"NVDA": df_nvda, "SPY": spy_bear_enriched}
    candidates_uni_spy = screener.scan_universe(universe_with_bear_spy, quotes=quotes)
    assert candidates_uni_spy == []

    # 6. Healthy bull SPY -> successfully passes
    candidates_ok = screener.scan_universe(universe, spy_df=spy_bull_enriched, quotes=quotes)
    assert len(candidates_ok) == 1
    assert candidates_ok[0].ticker == "NVDA"


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


def test_liquidity_prefilter_with_execution_quote_spread_rejection():
    """Test spread rejection when bid/ask spread exceeds threshold (> 6 bps) and validation."""
    screener = QuantitativeScreener(max_spread_rel=0.0006)  # 6 bps
    df = generate_synthetic_bars(50, base_price=100.0, volume_base=500_000.0)
    df = MarketDataPipeline.compute_indicators(df)
    now = datetime.now(timezone.utc)

    # 1. Tight quote (2 bps spread: bid 100.00, ask 100.02) -> passes
    q_tight = ExecutionQuote(ticker="TIGHT", bid=100.00, ask=100.02, timestamp=now)
    ok, msg = screener.check_liquidity_prefilter(df, quote=q_tight)
    assert ok is True
    assert "Passed" in msg

    # 2. Wide spread quote (> 6 bps, e.g. 10 bps: bid 100.00, ask 100.10) -> rejected
    q_wide = ExecutionQuote(ticker="WIDE", bid=100.00, ask=100.10, timestamp=now)
    ok_wide, msg_wide = screener.check_liquidity_prefilter(df, quote=q_wide)
    assert ok_wide is False
    assert "bps >" in msg_wide

    # 3. Invalid quote (inverted ask < bid) -> rejected
    q_invalid = ExecutionQuote(ticker="INV", bid=105.00, ask=100.00, timestamp=now)
    ok_inv, msg_inv = screener.check_liquidity_prefilter(df, quote=q_invalid)
    assert ok_inv is False
    assert "Invalid execution quote" in msg_inv

    # 4. Stale quote (> 60s max quote age) -> rejected
    stale_time = now - timedelta(seconds=90)
    q_stale = ExecutionQuote(ticker="STALE", bid=100.00, ask=100.02, timestamp=stale_time)
    ok_stale, msg_stale = screener.check_liquidity_prefilter(df, quote=q_stale)
    assert ok_stale is False
    assert "Stale execution quote" in msg_stale

    # 5. Missing quote when required for execution -> fail closed
    ok_req, msg_req = screener.check_liquidity_prefilter(df, quote=None, require_quote=True)
    assert ok_req is False
    assert "Missing execution quote" in msg_req


def test_entry_pricing_derived_from_ask_price_for_buys():
    """Verify entry pricing is derived from ask price for buys instead of bar close."""
    screener = QuantitativeScreener()
    now = datetime.now(timezone.utc)

    # 1. Trend Pullback setup
    df_trend = generate_synthetic_bars(220, base_price=100.0, trend_slope=0.3, volume_base=1_000_000.0)
    df_trend = MarketDataPipeline.compute_indicators(df_trend)

    ema20 = df_trend["ema_20"].iloc[-1]
    df_trend.iloc[-2, df_trend.columns.get_loc("low")] = ema20 * 0.995
    df_trend.iloc[-2, df_trend.columns.get_loc("high")] = ema20 * 1.02
    df_trend.iloc[-2, df_trend.columns.get_loc("close")] = ema20 * 1.00

    df_trend.iloc[-1, df_trend.columns.get_loc("open")] = ema20 * 1.00
    df_trend.iloc[-1, df_trend.columns.get_loc("low")] = ema20 * 0.998
    df_trend.iloc[-1, df_trend.columns.get_loc("high")] = ema20 * 1.05
    df_trend.iloc[-1, df_trend.columns.get_loc("close")] = ema20 * 1.04
    df_trend.iloc[-1, df_trend.columns.get_loc("volume")] = df_trend["volume"].iloc[-2] * 2.0
    df_trend.iloc[-1, df_trend.columns.get_loc("rvol_20")] = 1.65
    df_trend.iloc[-1, df_trend.columns.get_loc("rs_spy_63d")] = 1.15

    close_price = df_trend["close"].iloc[-1]
    # Provide execution quote with ask higher than close
    ask_price = round(close_price + 0.35, 2)
    bid_price = round(close_price + 0.33, 2)
    quote = ExecutionQuote(ticker="NVDA", bid=bid_price, ask=ask_price, timestamp=now)

    cand_trend = screener.evaluate_trend_pullback("NVDA", df_trend, quote=quote)
    assert cand_trend is not None
    # Entry pricing MUST be derived from ask price for buys
    assert cand_trend.entry_est == pytest.approx(ask_price, abs=0.01)
    assert cand_trend.entry_est != pytest.approx(close_price, abs=0.01)

    # 2. Mean Reversion setup
    df_mr = generate_synthetic_bars(220, base_price=120.0, trend_slope=0.1, volume_base=1_000_000.0)
    df_mr = MarketDataPipeline.compute_indicators(df_mr)

    df_mr.iloc[-1, df_mr.columns.get_loc("rsi_14")] = 28.0
    sma_200 = df_mr["sma_200"].iloc[-1]
    curr_base = sma_200 + 10.0
    df_mr.iloc[-2, df_mr.columns.get_loc("high")] = curr_base - 0.5
    df_mr.iloc[-1, df_mr.columns.get_loc("low")] = curr_base - 5.0
    df_mr.iloc[-1, df_mr.columns.get_loc("high")] = curr_base + 1.0
    df_mr.iloc[-1, df_mr.columns.get_loc("open")] = curr_base - 1.0
    df_mr.iloc[-1, df_mr.columns.get_loc("close")] = curr_base + 0.5

    mr_close = df_mr["close"].iloc[-1]
    mr_ask = round(mr_close + 0.25, 2)
    mr_bid = round(mr_close + 0.23, 2)
    quote_mr = ExecutionQuote(ticker="GOOGL", bid=mr_bid, ask=mr_ask, timestamp=now)

    cand_mr = screener.evaluate_mean_reversion("GOOGL", df_mr, quote=quote_mr)
    assert cand_mr is not None
    # Entry pricing MUST be derived from ask price for buys
    assert cand_mr.entry_est == pytest.approx(mr_ask, abs=0.01)
    assert cand_mr.entry_est != pytest.approx(mr_close, abs=0.01)


def test_scan_universe_filters_wide_spread_with_execution_quotes():
    """Verify scan_universe filters out candidates with wide execution quote spreads."""
    screener = QuantitativeScreener(max_spread_rel=0.0006)
    now = datetime.now(timezone.utc)

    # Construct two universe tickers
    df_nvda = generate_synthetic_bars(220, base_price=100.0, trend_slope=0.3, volume_base=1_000_000.0)
    df_nvda = MarketDataPipeline.compute_indicators(df_nvda)
    ema20 = df_nvda["ema_20"].iloc[-1]
    df_nvda.iloc[-2, df_nvda.columns.get_loc("low")] = ema20 * 0.995
    df_nvda.iloc[-2, df_nvda.columns.get_loc("high")] = ema20 * 1.02
    df_nvda.iloc[-2, df_nvda.columns.get_loc("close")] = ema20 * 1.00
    df_nvda.iloc[-1, df_nvda.columns.get_loc("open")] = ema20 * 1.00
    df_nvda.iloc[-1, df_nvda.columns.get_loc("low")] = ema20 * 0.998
    df_nvda.iloc[-1, df_nvda.columns.get_loc("high")] = ema20 * 1.05
    df_nvda.iloc[-1, df_nvda.columns.get_loc("close")] = ema20 * 1.04
    df_nvda.iloc[-1, df_nvda.columns.get_loc("volume")] = df_nvda["volume"].iloc[-2] * 2.0
    df_nvda.iloc[-1, df_nvda.columns.get_loc("rvol_20")] = 1.65
    df_nvda.iloc[-1, df_nvda.columns.get_loc("rs_spy_63d")] = 1.15

    # NVDA gets wide spread (> 6 bps) -> should be excluded
    q_wide = ExecutionQuote(ticker="NVDA", bid=100.00, ask=100.15, timestamp=now)

    universe = {"NVDA": df_nvda}
    quotes = {"NVDA": q_wide}

    candidates = screener.scan_universe(universe, quotes=quotes)
    assert len(candidates) == 0

    # Now provide tight spread (< 6 bps) -> should be accepted
    q_tight = ExecutionQuote(ticker="NVDA", bid=100.00, ask=100.02, timestamp=now)
    quotes_tight = {"NVDA": q_tight}
    candidates_ok = screener.scan_universe(universe, quotes=quotes_tight)
    assert len(candidates_ok) == 1
    assert candidates_ok[0].ticker == "NVDA"
    assert candidates_ok[0].entry_est == pytest.approx(100.02, abs=0.01)
