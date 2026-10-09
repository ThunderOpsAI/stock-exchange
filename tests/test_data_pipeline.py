"""
Unit and regression tests for Market Data Pipeline.
Covers typed market-data result models, provenance tracking, freshness validation,
and fail-closed error handling (ADR 0002).
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

pytestmark = pytest.mark.unit

from src.data.pipeline import MarketDataPipeline
from src.domain.models import (
    DataFreshness,
    DataIntegrityError,
    DataProvenance,
    ExecutionQuote,
    MarketDataHealth,
    MarketDataResult,
    QuoteData,
)


def create_synthetic_daily_df(num_bars: int = 30) -> pd.DataFrame:
    """Helper to generate a valid daily OHLCV dataframe."""
    now = datetime.now(timezone.utc)
    dates = [now - timedelta(days=num_bars - i) for i in range(num_bars)]
    closes = [100.0 + i * 0.5 for i in range(num_bars)]
    return pd.DataFrame(
        {
            "open": [c - 0.2 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1_000_000.0 for _ in range(num_bars)],
        },
        index=dates,
    )


@pytest.fixture
def temp_pipeline(tmp_path: Path) -> MarketDataPipeline:
    cache_dir = tmp_path / "market_data"
    news_dir = tmp_path / "news"
    return MarketDataPipeline(cache_dir=cache_dir, news_cache_dir=news_dir)


def test_successful_fresh_data_fetch_yfinance(temp_pipeline: MarketDataPipeline):
    """Test successful provider fetch returns HEALTHY result with yfinance provenance."""
    sample_df = create_synthetic_daily_df(30)
    yf_df = sample_df.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )

    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = yf_df
        mock_ticker_cls.return_value = mock_ticker

        result = temp_pipeline.fetch_daily_bars_result("AAPL")

        assert result.status == MarketDataHealth.HEALTHY
        assert result.freshness == DataFreshness.FRESH
        assert result.ticker == "AAPL"
        assert result.bars_count == 30
        assert result.df is not None
        assert len(result.df) == 30
        assert result.provenance.source == "yfinance"
        assert result.provenance.ticker == "AAPL"
        assert result.provenance.status == DataFreshness.FRESH
        assert result.provenance.cache_hit is False
        assert result.provenance.as_of_time is not None
        assert result.provenance.latency_ms is not None

        # Verify disk cache file was populated
        cache_file = temp_pipeline.cache_dir / "AAPL_daily.csv"
        assert cache_file.exists()


def test_successful_fresh_cache_hit(temp_pipeline: MarketDataPipeline):
    """Test that valid, fresh cache is returned with local_cache provenance."""
    sample_df = create_synthetic_daily_df(40)
    cache_file = temp_pipeline.cache_dir / "MSFT_daily.csv"
    sample_df.to_csv(cache_file)

    with patch("yfinance.Ticker") as mock_ticker_cls:
        result = temp_pipeline.fetch_daily_bars_result("MSFT")

        # Provider should not be called on cache hit
        mock_ticker_cls.assert_not_called()
        assert result.status == MarketDataHealth.HEALTHY
        assert result.freshness == DataFreshness.FRESH
        assert result.provenance.source == "local_cache"
        assert result.provenance.cache_hit is True
        assert result.provenance.ticker == "MSFT"
        assert result.bars_count == 40
        assert result.df is not None


def test_stale_cache_rejection_fail_closed(temp_pipeline: MarketDataPipeline):
    """Test that when cache is stale and provider fails, fail-closed policy blocks stale cache fallback."""
    sample_df = create_synthetic_daily_df(30)
    cache_file = temp_pipeline.cache_dir / "NVDA_daily.csv"
    sample_df.to_csv(cache_file)

    # Set cache file modification time to 25 hours ago (> 12 hours)
    old_time = time.time() - (25 * 3600)
    os.utime(cache_file, (old_time, old_time))

    # Mock provider failure
    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = ConnectionError("Provider network unreachable")
        mock_ticker_cls.return_value = mock_ticker

        result = temp_pipeline.fetch_daily_bars_result("NVDA")

        # Must fail closed: UNHEALTHY/ERROR, no df returned
        assert result.status == MarketDataHealth.ERROR
        assert result.freshness == DataFreshness.ERROR
        assert result.df is None
        assert "STALE_CACHE_BLOCKED" in result.error_classification
        assert "fail-closed policy" in result.error_reason

        # Backwards compatible fetch_daily_bars in non-strict mode returns empty df (no stale cache)
        legacy_df = temp_pipeline.fetch_daily_bars("NVDA", strict=False)
        assert legacy_df.empty

        # In strict mode, fetch_daily_bars raises DataIntegrityError
        with pytest.raises(DataIntegrityError) as exc_info:
            temp_pipeline.fetch_daily_bars("NVDA", strict=True)
        assert "fail-closed" in str(exc_info.value).lower()


def test_corrupted_cache_fail_closed(temp_pipeline: MarketDataPipeline):
    """Test corrupted or unparseable cache file is rejected safely."""
    cache_file = temp_pipeline.cache_dir / "CORRUPT_daily.csv"
    cache_file.write_text("NOT,A,VALID,CSV\n1,2,3")

    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = TimeoutError("Timed out contacting yfinance")
        mock_ticker_cls.return_value = mock_ticker

        result = temp_pipeline.fetch_daily_bars_result("CORRUPT")
        assert result.status == MarketDataHealth.ERROR
        assert result.df is None
        assert "TIMEOUT" in result.error_classification

        with pytest.raises(DataIntegrityError):
            temp_pipeline.fetch_daily_bars("CORRUPT", strict=True)


def test_cache_missing_required_columns(temp_pipeline: MarketDataPipeline):
    """Test cache missing required columns (e.g., volume missing) is classified as corrupted."""
    df_missing_col = pd.DataFrame(
        {
            "open": [10.0, 11.0, 12.0, 13.0, 14.0],
            "high": [11.0, 12.0, 13.0, 14.0, 15.0],
            "low": [9.0, 10.0, 11.0, 12.0, 13.0],
            "close": [10.5, 11.5, 12.5, 13.5, 14.5],
            # 'volume' is intentionally omitted
        },
        index=pd.date_range("2026-01-01", periods=5, tz="UTC"),
    )
    cache_file = temp_pipeline.cache_dir / "BADCOLS_daily.csv"
    df_missing_col.to_csv(cache_file)

    freshness, df, msg = temp_pipeline.check_cache_freshness("BADCOLS")
    assert freshness == DataFreshness.ERROR
    assert "missing required columns" in msg.lower()


def test_provider_timeout_error(temp_pipeline: MarketDataPipeline):
    """Test provider timeout returns ERROR result with classified failure."""
    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = TimeoutError("Connection to yfinance timed out")
        mock_ticker_cls.return_value = mock_ticker

        result = temp_pipeline.fetch_daily_bars_result("TIMEOUT")
        assert result.status == MarketDataHealth.ERROR
        assert result.freshness == DataFreshness.ERROR
        assert result.error_classification == "TIMEOUT"
        assert "timed out" in result.error_reason.lower()
        assert result.df is None

        # Verify strict mode raises DataIntegrityError
        with pytest.raises(DataIntegrityError) as exc_info:
            temp_pipeline.fetch_daily_bars("TIMEOUT", strict=True)
        assert "TIMEOUT" in str(exc_info.value)


def test_provider_network_error(temp_pipeline: MarketDataPipeline):
    """Test provider network error classification."""
    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = ConnectionResetError("Connection reset by peer")
        mock_ticker_cls.return_value = mock_ticker

        result = temp_pipeline.fetch_daily_bars_result("NETERR")
        assert result.status == MarketDataHealth.ERROR
        assert result.error_classification == "NETWORK_ERROR"
        assert result.df is None


def test_provider_empty_payload(temp_pipeline: MarketDataPipeline):
    """Test provider returning empty DataFrame returns UNHEALTHY result."""
    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mock_ticker_cls.return_value = mock_ticker

        result = temp_pipeline.fetch_daily_bars_result("EMPTY")
        assert result.status == MarketDataHealth.UNHEALTHY
        assert result.freshness == DataFreshness.UNHEALTHY
        assert result.error_classification == "EMPTY_PAYLOAD"
        assert result.bars_count == 0
        assert result.df is None


def test_provider_insufficient_bars(temp_pipeline: MarketDataPipeline):
    """Test provider returning fewer than minimum bars (5) returns UNHEALTHY."""
    sample_df = create_synthetic_daily_df(3)
    yf_df = sample_df.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )

    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = yf_df
        mock_ticker_cls.return_value = mock_ticker

        result = temp_pipeline.fetch_daily_bars_result("FEW")
        assert result.status == MarketDataHealth.UNHEALTHY
        assert result.freshness == DataFreshness.UNHEALTHY
        assert result.error_classification == "INSUFFICIENT_BARS"
        assert result.df is None


def test_provider_malformed_payload_missing_columns(temp_pipeline: MarketDataPipeline):
    """Test provider returning DataFrame without required columns returns ERROR."""
    malformed_df = pd.DataFrame(
        {"close": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]},
        index=pd.date_range("2026-01-01", periods=6, tz="UTC"),
    )

    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = malformed_df
        mock_ticker_cls.return_value = mock_ticker

        result = temp_pipeline.fetch_daily_bars_result("MALFORMED")
        assert result.status == MarketDataHealth.ERROR
        assert result.freshness == DataFreshness.ERROR
        assert result.error_classification == "MALFORMED_PAYLOAD"
        assert "missing required columns" in result.error_reason.lower()
        assert result.df is None


def test_provenance_metadata_model():
    """Verify DataProvenance and MarketDataResult models and field constraints."""
    now = datetime.now(timezone.utc)
    prov = DataProvenance(
        source="yfinance",
        ticker="SPY",
        timestamp=now,
        as_of_time=now,
        status=DataFreshness.FRESH,
        latency_ms=12.5,
        cache_hit=False,
        details="Sample details",
    )
    assert prov.source == "yfinance"
    assert prov.ticker == "SPY"
    assert prov.status == DataFreshness.FRESH
    assert prov.cache_hit is False
    assert prov.latency_ms == 12.5

    res = MarketDataResult(
        ticker="SPY",
        status=MarketDataHealth.HEALTHY,
        freshness=DataFreshness.FRESH,
        provenance=prov,
        bars_count=100,
    )
    assert res.status == MarketDataHealth.HEALTHY
    assert res.bars_count == 100
    assert res.error_reason is None


def test_news_headlines_fresh_cache_and_stale_rejection(temp_pipeline: MarketDataPipeline):
    """Test news headline cache freshness and fail-closed handling."""
    news_cache = temp_pipeline.news_cache_dir / "GOOG_news.json"
    dummy_news = [{"title": "Q3 earnings up", "publisher": "Reuters", "published_at": "2026-10-01"}]
    with open(news_cache, "w") as f:
        json.dump(dummy_news, f)

    # 1. Fresh cache returns headlines without provider call
    with patch("yfinance.Ticker") as mock_ticker_cls:
        headlines = temp_pipeline.fetch_news_headlines("GOOG")
        mock_ticker_cls.assert_not_called()
        assert len(headlines) == 1
        assert headlines[0]["title"] == "Q3 earnings up"

    # 2. Age cache to 8 hours (> 6 hours threshold)
    old_time = time.time() - (8 * 3600)
    os.utime(news_cache, (old_time, old_time))

    # Mock provider failure: must NOT fall back to stale news cache
    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.news = None
        type(mock_ticker).news = MagicMock(side_effect=TimeoutError("News API timeout"))
        mock_ticker_cls.return_value = mock_ticker

        headlines = temp_pipeline.fetch_news_headlines("GOOG")
        assert headlines == []  # fail closed!


def test_execution_quote_validation_and_spread_calculation():
    """Verify ExecutionQuote validation (bid > 0, ask >= bid, spread calculation)."""
    now = datetime.now(timezone.utc)

    # 1. Valid quote with tight spread
    q_valid = ExecutionQuote(
        ticker="AAPL",
        bid=150.00,
        ask=150.05,
        bid_size=500.0,
        ask_size=600.0,
        timestamp=now,
    )
    assert q_valid.is_valid is True
    assert q_valid.spread_usd == pytest.approx(0.05)
    assert q_valid.spread_rel == pytest.approx(0.05 / 150.05)
    assert q_valid.spread_bps == pytest.approx((0.05 / 150.05) * 10000.0)

    # validate_quote helper
    ok, msg = q_valid.validate_quote(max_age_seconds=60.0)
    assert ok is True
    assert "valid" in msg.lower()

    # QuoteData alias
    assert QuoteData is ExecutionQuote

    # 2. Non-positive bid price (bid <= 0)
    q_zero_bid = ExecutionQuote(ticker="ZERO", bid=0.0, ask=10.0, timestamp=now)
    assert q_zero_bid.is_valid is False
    ok, msg = q_zero_bid.validate_quote()
    assert ok is False
    assert "non-positive bid" in msg.lower() or "invalid" in msg.lower()

    q_neg_bid = ExecutionQuote(ticker="NEG", bid=-5.0, ask=10.0, timestamp=now)
    assert q_neg_bid.is_valid is False

    # 3. Inverted quote (ask < bid)
    q_inverted = ExecutionQuote(ticker="INV", bid=105.0, ask=100.0, timestamp=now)
    assert q_inverted.is_valid is False
    ok, msg = q_inverted.validate_quote()
    assert ok is False
    assert "inverted" in msg.lower() or "invalid" in msg.lower()

    # 4. Zero spread quote (ask == bid)
    q_zero_spread = ExecutionQuote(ticker="FLAT", bid=100.0, ask=100.0, timestamp=now)
    assert q_zero_spread.is_valid is True
    assert q_zero_spread.spread_usd == 0.0
    assert q_zero_spread.spread_rel == 0.0
    assert q_zero_spread.spread_bps == 0.0

    # 5. Stale quote validation check
    stale_time = now - timedelta(seconds=120)
    q_stale = ExecutionQuote(ticker="STALE", bid=100.0, ask=100.05, timestamp=stale_time)
    assert q_stale.is_valid is True
    ok_stale, msg_stale = q_stale.validate_quote(max_age_seconds=60.0)
    assert ok_stale is False
    assert "stale" in msg_stale.lower()


def test_pipeline_fetch_execution_quote_injection_and_synthesis(temp_pipeline: MarketDataPipeline):
    """Test injecting and synthesizing quotes in MarketDataPipeline."""
    now = datetime.now(timezone.utc)

    # 1. Synthesize quote
    synth_q = temp_pipeline.synthesize_execution_quote(
        ticker="SPY",
        mid_price=450.0,
        spread_bps=2.0,
        bid_size=1000.0,
        ask_size=1200.0,
        timestamp=now,
    )
    assert synth_q.ticker == "SPY"
    assert synth_q.bid > 0
    assert synth_q.ask >= synth_q.bid
    assert synth_q.is_valid is True
    assert synth_q.spread_bps == pytest.approx(2.0, abs=0.1)

    # 2. Inject quote into pipeline
    temp_pipeline.inject_quote(synth_q)

    # Fetch quote via fetch_execution_quote and alias fetch_quote_result
    fetched_1 = temp_pipeline.fetch_execution_quote("SPY")
    assert fetched_1 is not None
    assert fetched_1.ticker == "SPY"
    assert fetched_1.bid == synth_q.bid
    assert fetched_1.ask == synth_q.ask

    fetched_2 = temp_pipeline.fetch_quote_result("SPY")
    assert fetched_2 is not None
    assert fetched_2.ticker == "SPY"
    assert fetched_2.spread_usd == synth_q.spread_usd

    # 3. Clear injected quotes
    temp_pipeline.clear_injected_quotes()
    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.fast_info = None
        mock_ticker.info = None
        mock_ticker_cls.return_value = mock_ticker

        after_clear = temp_pipeline.fetch_execution_quote("SPY")
        assert after_clear is None


def test_pipeline_execution_quote_fails_closed_no_estimated_spread():
    """
    Ensure estimated spread (hardcoded 3 bps) is never used for execution decisions.
    Missing/unavailable live quote must fail closed with None or DataIntegrityError.
    """
    pipeline = MarketDataPipeline()

    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.fast_info = None
        mock_ticker.info = None
        mock_ticker_cls.return_value = mock_ticker

        # Non-strict mode returns None (no ghost quote or estimated spread)
        quote = pipeline.fetch_execution_quote("UNKNOWN")
        assert quote is None

        # Strict mode raises DataIntegrityError
        with pytest.raises(DataIntegrityError) as exc_info:
            pipeline.fetch_execution_quote("UNKNOWN", strict=True)
        assert "fail-closed" in str(exc_info.value).lower()
        assert "estimated spread prohibited" in str(exc_info.value).lower()


def test_pipeline_fetch_execution_quote_with_quote_provider():
    """Verify MarketDataPipeline with custom quote provider/broker adapter."""
    mock_quote = ExecutionQuote(
        ticker="IBM",
        bid=140.0,
        ask=140.04,
        timestamp=datetime.now(timezone.utc),
    )
    mock_provider = MagicMock(return_value=mock_quote)
    pipeline = MarketDataPipeline(quote_provider=mock_provider)

    quote = pipeline.fetch_execution_quote("IBM")
    assert quote is not None
    assert quote.ticker == "IBM"
    assert quote.bid == 140.0
    assert quote.ask == 140.04
    mock_provider.assert_called_once_with("IBM")
