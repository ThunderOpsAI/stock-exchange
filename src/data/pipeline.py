"""
Market Data Pipeline.
Fetches daily OHLCV and news headlines via yfinance / Finnhub with local caching.
Computes technical indicators: SMA50, SMA200, EMA10, EMA20, ATR14, RSI14, RVOL20, RS_SPY_63d, ADDV20.
Adheres strictly to ADR 0002 fail-closed semantics and typed data provenance.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
import yfinance as yf

from src.domain.models import (
    DataFreshness,
    DataIntegrityError,
    DataProvenance,
    ExecutionQuote,
    MarketDataHealth,
    MarketDataResult,
)

logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache/market_data")
NEWS_CACHE_DIR = Path("data/cache/news")


class MarketDataPipeline:
    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        news_cache_dir: Optional[Path] = None,
        max_cache_age_hours: float = 12.0,
        max_news_cache_age_hours: float = 6.0,
        quote_provider: Optional[Any] = None,
    ):
        self.cache_dir = Path(cache_dir or CACHE_DIR)
        self.news_cache_dir = Path(news_cache_dir or NEWS_CACHE_DIR)
        self.max_cache_age_hours = max_cache_age_hours
        self.max_news_cache_age_hours = max_news_cache_age_hours
        self.quote_provider = quote_provider
        self._injected_quotes: Dict[str, ExecutionQuote] = {}
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.news_cache_dir.mkdir(parents=True, exist_ok=True)

    def check_cache_freshness(
        self,
        ticker: str,
        max_cache_age_hours: Optional[float] = None,
    ) -> Tuple[DataFreshness, Optional[pd.DataFrame], Optional[str]]:
        """
        Inspects the local cache file for a ticker.
        Returns:
            (DataFreshness, DataFrame or None, reason_or_error_str)
        """
        ticker = ticker.upper()
        cache_file = self.cache_dir / f"{ticker}_daily.csv"
        if not cache_file.exists():
            return DataFreshness.UNHEALTHY, None, "Cache file does not exist"

        max_age = timedelta(
            hours=max_cache_age_hours if max_cache_age_hours is not None else self.max_cache_age_hours
        )
        now_utc = datetime.now(timezone.utc)
        try:
            mtime = datetime.fromtimestamp(cache_file.stat().st_mtime, tz=timezone.utc)
        except OSError as exc:
            return DataFreshness.ERROR, None, f"Failed to read cache file metadata: {exc}"

        age = now_utc - mtime

        try:
            df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
        except Exception as exc:
            return DataFreshness.ERROR, None, f"Corrupted cache file: {exc}"

        if df.empty or len(df) < 5:
            return DataFreshness.UNHEALTHY, None, f"Cache contains insufficient bars ({len(df)})"

        required_cols = {"open", "high", "low", "close", "volume"}
        if not required_cols.issubset(df.columns):
            missing = required_cols - set(df.columns)
            return DataFreshness.ERROR, None, f"Cache missing required columns: {missing}"

        if isinstance(df.index, pd.DatetimeIndex):
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            else:
                df.index = df.index.tz_convert("UTC")

        if age > max_age:
            return DataFreshness.STALE, df, f"Cache age ({age}) exceeds max allowed ({max_age})"

        return DataFreshness.FRESH, df, None

    def fetch_daily_bars_result(
        self,
        ticker: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: int = 365,
        use_cache: bool = True,
        max_cache_age_hours: Optional[float] = None,
    ) -> MarketDataResult:
        """
        Fetches daily bars with explicit health status, data provenance, and error classification.
        Fails closed on stale cache, provider timeout, provider network failure, or malformed data.
        """
        ticker = ticker.upper()
        now_utc = datetime.now(timezone.utc)
        start_t = time.perf_counter()
        cache_file = self.cache_dir / f"{ticker}_daily.csv"

        cached_stale_df: Optional[pd.DataFrame] = None
        cache_corrupted: bool = False
        cache_reason: Optional[str] = None

        if use_cache and cache_file.exists():
            freshness, cached_df, cache_msg = self.check_cache_freshness(
                ticker, max_cache_age_hours=max_cache_age_hours
            )
            if freshness == DataFreshness.FRESH and cached_df is not None:
                latency_ms = (time.perf_counter() - start_t) * 1000.0
                as_of = None
                if len(cached_df) > 0 and hasattr(cached_df.index[-1], "to_pydatetime"):
                    as_of = cached_df.index[-1].to_pydatetime()
                    if as_of.tzinfo is None:
                        as_of = as_of.replace(tzinfo=timezone.utc)

                provenance = DataProvenance(
                    source="local_cache",
                    ticker=ticker,
                    timestamp=now_utc,
                    as_of_time=as_of,
                    status=DataFreshness.FRESH,
                    latency_ms=latency_ms,
                    cache_hit=True,
                    details=f"Loaded from cache ({cache_file.name})",
                )
                return MarketDataResult(
                    ticker=ticker,
                    status=MarketDataHealth.HEALTHY,
                    freshness=DataFreshness.FRESH,
                    provenance=provenance,
                    df=cached_df,
                    bars_count=len(cached_df),
                )
            elif freshness == DataFreshness.STALE:
                cached_stale_df = cached_df
                cache_reason = cache_msg
            elif freshness == DataFreshness.ERROR:
                cache_corrupted = True
                cache_reason = cache_msg

        # Must fetch fresh data from provider (yfinance)
        try:
            if not end_date:
                end_date_dt = now_utc
            else:
                end_date_dt = datetime.fromisoformat(end_date)
                if end_date_dt.tzinfo is None:
                    end_date_dt = end_date_dt.replace(tzinfo=timezone.utc)

            if not start_date:
                start_date_dt = end_date_dt - timedelta(days=days)
            else:
                start_date_dt = datetime.fromisoformat(start_date)
                if start_date_dt.tzinfo is None:
                    start_date_dt = start_date_dt.replace(tzinfo=timezone.utc)

            ticker_obj = yf.Ticker(ticker)
            df = ticker_obj.history(
                start=start_date_dt.strftime("%Y-%m-%d"),
                end=end_date_dt.strftime("%Y-%m-%d"),
                interval="1d",
            )
            latency_ms = (time.perf_counter() - start_t) * 1000.0

            if df is None or not isinstance(df, pd.DataFrame):
                return self._build_failure_result(
                    ticker=ticker,
                    status=MarketDataHealth.ERROR,
                    freshness=DataFreshness.ERROR,
                    classification="MALFORMED_PAYLOAD",
                    reason=f"Provider returned invalid non-DataFrame payload for {ticker}",
                    source="yfinance",
                    latency_ms=latency_ms,
                    stale_cache_present=cached_stale_df is not None,
                )

            if df.empty or len(df) < 5:
                bars_len = len(df) if df is not None else 0
                return self._build_failure_result(
                    ticker=ticker,
                    status=MarketDataHealth.UNHEALTHY,
                    freshness=DataFreshness.UNHEALTHY,
                    classification="EMPTY_PAYLOAD" if bars_len == 0 else "INSUFFICIENT_BARS",
                    reason=f"Provider returned insufficient bars ({bars_len} bars) for {ticker}",
                    source="yfinance",
                    latency_ms=latency_ms,
                    stale_cache_present=cached_stale_df is not None,
                )

            # Normalize column names
            df = df.rename(
                columns={
                    "Open": "open",
                    "High": "high",
                    "Low": "low",
                    "Close": "close",
                    "Volume": "volume",
                }
            )
            required_cols = ["open", "high", "low", "close", "volume"]
            missing_cols = [c for c in required_cols if c not in df.columns]
            if missing_cols:
                return self._build_failure_result(
                    ticker=ticker,
                    status=MarketDataHealth.ERROR,
                    freshness=DataFreshness.ERROR,
                    classification="MALFORMED_PAYLOAD",
                    reason=f"Provider payload missing required columns: {missing_cols}",
                    source="yfinance",
                    latency_ms=latency_ms,
                    stale_cache_present=cached_stale_df is not None,
                )

            df = df[required_cols]

            if isinstance(df.index, pd.DatetimeIndex):
                if df.index.tz is None:
                    df.index = df.index.tz_localize("UTC")
                else:
                    df.index = df.index.tz_convert("UTC")

            # Persist fresh payload to cache
            try:
                df.to_csv(cache_file)
            except Exception as write_err:
                logger.warning(f"Failed to persist market data cache for {ticker}: {write_err}")

            as_of = None
            if len(df) > 0 and hasattr(df.index[-1], "to_pydatetime"):
                as_of = df.index[-1].to_pydatetime()
                if as_of.tzinfo is None:
                    as_of = as_of.replace(tzinfo=timezone.utc)

            provenance = DataProvenance(
                source="yfinance",
                ticker=ticker,
                timestamp=now_utc,
                as_of_time=as_of,
                status=DataFreshness.FRESH,
                latency_ms=latency_ms,
                cache_hit=False,
                details=f"Retrieved {len(df)} bars from yfinance",
            )
            return MarketDataResult(
                ticker=ticker,
                status=MarketDataHealth.HEALTHY,
                freshness=DataFreshness.FRESH,
                provenance=provenance,
                df=df,
                bars_count=len(df),
            )

        except Exception as exc:
            latency_ms = (time.perf_counter() - start_t) * 1000.0
            classification = self._classify_exception(exc)
            if cached_stale_df is not None:
                reason = (
                    f"Provider fetch failed with {classification}: {exc}; "
                    f"stale cache fallback blocked by fail-closed policy (ADR 0002). "
                    f"Prior cache reason: {cache_reason}"
                )
            elif cache_corrupted:
                reason = (
                    f"Provider fetch failed with {classification}: {exc}; "
                    f"cache is corrupted: {cache_reason}"
                )
            else:
                reason = f"Provider request failed for {ticker}: {exc}"

            return self._build_failure_result(
                ticker=ticker,
                status=MarketDataHealth.ERROR,
                freshness=DataFreshness.ERROR,
                classification=classification,
                reason=reason,
                source="yfinance",
                latency_ms=latency_ms,
                stale_cache_present=cached_stale_df is not None,
            )

    def fetch_daily_bars(
        self,
        ticker: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: int = 365,
        use_cache: bool = True,
        strict: bool = False,
    ) -> pd.DataFrame:
        """
        Retrieves daily bars. Backward compatible with legacy callers.
        If healthy: returns pd.DataFrame with bars.
        If unhealthy/error:
            strict=True: raises DataIntegrityError
            strict=False: returns empty pd.DataFrame (fail closed, no stale cache)
        """
        result = self.fetch_daily_bars_result(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
            days=days,
            use_cache=use_cache,
        )
        if result.status == MarketDataHealth.HEALTHY and result.df is not None:
            return result.df

        if strict:
            raise DataIntegrityError(
                f"Market data integrity error for {ticker}: {result.error_reason} "
                f"[{result.error_classification or 'UNKNOWN'}]"
            )

        logger.warning(
            f"fetch_daily_bars failed closed for {ticker}: {result.error_reason} "
            f"(classification={result.error_classification}, status={result.status.value})"
        )
        return pd.DataFrame()

    def inject_quote(self, quote: ExecutionQuote) -> None:
        """Injects a typed execution quote for testing or simulated environments."""
        self._injected_quotes[quote.ticker.upper()] = quote

    def inject_execution_quote(self, quote: ExecutionQuote) -> None:
        """Alias for inject_quote."""
        self.inject_quote(quote)

    def clear_injected_quotes(self) -> None:
        """Clears all injected quotes."""
        self._injected_quotes.clear()

    def synthesize_execution_quote(
        self,
        ticker: str,
        mid_price: float,
        spread_bps: float = 3.0,
        bid_size: float = 100.0,
        ask_size: float = 100.0,
        timestamp: Optional[datetime] = None,
    ) -> ExecutionQuote:
        """
        Synthesizes an execution quote from mid-price and spread in basis points.
        Explicitly intended ONLY for mock/testing or simulated replay environments.
        """
        ticker = ticker.upper()
        spread_fraction = spread_bps / 10000.0
        half_spread = (mid_price * spread_fraction) / 2.0
        bid = round(mid_price - half_spread, 4)
        ask = round(mid_price + half_spread, 4)
        return ExecutionQuote(
            ticker=ticker,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            timestamp=timestamp or datetime.now(timezone.utc),
        )

    def fetch_execution_quote(
        self,
        ticker: str,
        strict: bool = False,
        max_age_seconds: Optional[float] = None,
    ) -> Optional[ExecutionQuote]:
        """
        Fetches a typed execution quote, separating execution pricing from historical daily bars.
        Fails closed on missing or invalid quotes. Estimated historical spreads are NEVER used.
        """
        ticker = ticker.upper()

        # 1. Injected quote (mock / simulation)
        if ticker in self._injected_quotes:
            quote = self._injected_quotes[ticker]
            if not quote.is_valid:
                if strict:
                    raise DataIntegrityError(f"Injected execution quote for {ticker} is invalid")
                return quote
            if max_age_seconds is not None:
                valid, msg = quote.validate_quote(max_age_seconds=max_age_seconds)
                if not valid:
                    if strict:
                        raise DataIntegrityError(f"Injected execution quote for {ticker} is stale/invalid: {msg}")
                    return quote
            return quote

        # 2. Configured broker or quote provider
        if self.quote_provider is not None:
            try:
                if callable(self.quote_provider):
                    quote = self.quote_provider(ticker)
                elif hasattr(self.quote_provider, "get_quote"):
                    quote = self.quote_provider.get_quote(ticker)
                elif hasattr(self.quote_provider, "fetch_execution_quote"):
                    quote = self.quote_provider.fetch_execution_quote(ticker)
                else:
                    quote = None

                if quote is not None and isinstance(quote, ExecutionQuote):
                    if max_age_seconds is not None:
                        valid, msg = quote.validate_quote(max_age_seconds=max_age_seconds)
                        if not valid and strict:
                            raise DataIntegrityError(f"Provider quote for {ticker} failed validation: {msg}")
                    return quote
            except Exception as exc:
                if strict:
                    raise DataIntegrityError(f"Error fetching quote from provider for {ticker}: {exc}")
                logger.warning(f"Error fetching quote from provider for {ticker}: {exc}")
                return None

        # 3. Live quote from yfinance if available
        try:
            ticker_obj = yf.Ticker(ticker)
            fast_info = getattr(ticker_obj, "fast_info", None)
            info = getattr(ticker_obj, "info", None)

            bid = None
            ask = None

            if fast_info is not None:
                bid = getattr(fast_info, "bid", None) or (fast_info.get("bid") if isinstance(fast_info, dict) else None)
                ask = getattr(fast_info, "ask", None) or (fast_info.get("ask") if isinstance(fast_info, dict) else None)

            if (bid is None or ask is None) and isinstance(info, dict):
                bid = info.get("bid")
                ask = info.get("ask")

            if bid is not None and ask is not None:
                bid_f = float(bid)
                ask_f = float(ask)
                if bid_f > 0 and ask_f >= bid_f:
                    now_utc = datetime.now(timezone.utc)
                    return ExecutionQuote(
                        ticker=ticker,
                        bid=bid_f,
                        ask=ask_f,
                        timestamp=now_utc,
                    )
        except Exception as exc:
            logger.warning(f"Live provider quote fetch failed for {ticker}: {exc}")

        # Fail closed: NEVER fabricate or use estimated 3 bps spread for execution decisions
        if strict:
            raise DataIntegrityError(
                f"No valid live execution quote available for {ticker} (fail-closed, estimated spread prohibited)"
            )
        return None

    def fetch_quote_result(
        self,
        ticker: str,
        strict: bool = False,
        max_age_seconds: Optional[float] = None,
    ) -> Optional[ExecutionQuote]:
        """
        Alias for fetch_execution_quote to provide typed execution quotes.
        """
        return self.fetch_execution_quote(ticker=ticker, strict=strict, max_age_seconds=max_age_seconds)

    def fetch_news_headlines(self, ticker: str, days: int = 3) -> List[Dict[str, Any]]:
        ticker = ticker.upper()
        cache_file = self.news_cache_dir / f"{ticker}_news.json"

        if cache_file.exists():
            try:
                mtime = datetime.fromtimestamp(cache_file.stat().st_mtime, tz=timezone.utc)
                if datetime.now(timezone.utc) - mtime < timedelta(hours=self.max_news_cache_age_hours):
                    with open(cache_file, "r") as f:
                        data = json.load(f)
                        if isinstance(data, list):
                            return data
            except Exception as exc:
                logger.warning(f"Corrupted news cache file for {ticker}: {exc}")

        try:
            ticker_obj = yf.Ticker(ticker)
            raw_news = ticker_obj.news or []
            headlines = []
            for item in raw_news[:5]:
                content = item.get("content", {}) if isinstance(item, dict) and "content" in item else item
                if not isinstance(content, dict):
                    continue
                title = content.get("title") or item.get("title", "")
                pub_date = content.get("pubDate") or item.get("providerPublishTime")
                headlines.append(
                    {
                        "title": title,
                        "publisher": (
                            content.get("provider", {}).get("displayName")
                            if isinstance(content.get("provider"), dict)
                            else item.get("publisher", "")
                        ),
                        "published_at": str(pub_date),
                    }
                )
            if headlines:
                with open(cache_file, "w") as f:
                    json.dump(headlines, f)
            return headlines
        except Exception as exc:
            logger.warning(f"Failed to fetch news headlines for {ticker}: {exc}")
            # Fail closed: never fall back to stale news cache
            return []

    @staticmethod
    def _classify_exception(exc: Exception) -> str:
        exc_name = type(exc).__name__.lower()
        exc_str = str(exc).lower()
        if "timeout" in exc_name or "timeout" in exc_str:
            return "TIMEOUT"
        if "connection" in exc_name or "connection" in exc_str or "network" in exc_str:
            return "NETWORK_ERROR"
        if "http" in exc_name or "status" in exc_str:
            return "HTTP_ERROR"
        if "keyerror" in exc_name or "valueerror" in exc_name:
            return "MALFORMED_PAYLOAD"
        return "PROVIDER_ERROR"

    def _build_failure_result(
        self,
        ticker: str,
        status: MarketDataHealth,
        freshness: DataFreshness,
        classification: str,
        reason: str,
        source: str,
        latency_ms: Optional[float] = None,
        stale_cache_present: bool = False,
    ) -> MarketDataResult:
        if stale_cache_present and not classification.endswith("STALE_CACHE_BLOCKED"):
            classification = f"{classification}_STALE_CACHE_BLOCKED"

        provenance = DataProvenance(
            source=source,
            ticker=ticker,
            timestamp=datetime.now(timezone.utc),
            status=freshness,
            latency_ms=latency_ms,
            cache_hit=False,
            details=reason,
        )
        return MarketDataResult(
            ticker=ticker,
            status=status,
            freshness=freshness,
            provenance=provenance,
            df=None,
            bars_count=0,
            error_reason=reason,
            error_classification=classification,
        )

    @staticmethod
    def compute_indicators(df: pd.DataFrame, spy_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        if df.empty or len(df) < 20:
            return df

        df = df.copy()

        # Simple & Exponential Moving Averages
        df["sma_50"] = df["close"].rolling(window=50, min_periods=20).mean()
        df["sma_200"] = df["close"].rolling(window=200, min_periods=50).mean()
        df["ema_10"] = df["close"].ewm(span=10, adjust=False).mean()
        df["ema_20"] = df["close"].ewm(span=20, adjust=False).mean()

        # Average Daily Dollar Volume (20-day)
        df["addv_20"] = (df["close"] * df["volume"]).rolling(window=20, min_periods=5).mean()

        # Relative Volume (20-day)
        prior_vol_20 = df["volume"].shift(1).rolling(window=20, min_periods=5).mean()
        df["rvol_20"] = np.where(prior_vol_20 > 0, df["volume"] / prior_vol_20, 1.0)

        # Average True Range (Wilder 14-day)
        high_low = df["high"] - df["low"]
        high_prev_close = (df["high"] - df["close"].shift(1)).abs()
        low_prev_close = (df["low"] - df["close"].shift(1)).abs()
        tr = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)

        # Wilder's smoothing for ATR
        atr_14 = tr.ewm(alpha=1.0 / 14.0, adjust=False).mean()
        df["atr_14"] = atr_14
        df["atr_pct"] = (df["atr_14"] / df["close"]) * 100.0

        # Relative Strength Index (Wilder 14-day)
        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1.0 / 14.0, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1.0 / 14.0, adjust=False).mean()
        rs = np.where(avg_loss != 0, avg_gain / avg_loss, 100.0)
        df["rsi_14"] = 100.0 - (100.0 / (1.0 + rs))

        # Relative Strength vs SPY (63-day momentum ratio)
        if spy_df is not None and not spy_df.empty:
            spy_close = spy_df["close"].reindex(df.index, method="ffill")
            stock_63d_ret = df["close"] / df["close"].shift(63)
            spy_63d_ret = spy_close / spy_close.shift(63)
            df["rs_spy_63d"] = np.where(spy_63d_ret > 0, stock_63d_ret / spy_63d_ret, 1.0)
        else:
            stock_63d_ret = df["close"] / df["close"].shift(63)
            df["rs_spy_63d"] = stock_63d_ret.fillna(1.0)

        # Estimated relative spread (defaulting to liquid large-cap assumption 2-4 bps)
        df["spread_bps"] = 3.0
        df["spread_rel"] = df["spread_bps"] / 10000.0

        return df
