"""
Unit and regression tests for dynamic liquid universe construction and versioning.
Covers Ticket 27 (P5-04):
- UniverseEligibilityRecord captures price, liquidity (ADDV20 >= $25M), spread (<= 6 bps), tradability, halts, blackout.
- Explicit reason for inclusion/exclusion is recorded.
- Versioned universe snapshot with deterministic ID.
"""

from datetime import datetime, timezone
import pandas as pd
import pytest

from src.data.calendar import MarketCalendarGateService
from src.domain.models import ExecutionQuote
from src.screener.universe import (
    DynamicUniverseConstructor,
    DynamicUniverseVersion,
    UniverseEligibilityRecord,
)
from src.storage.db import Database


def _create_synthetic_bars(close_price: float, volume: float, n_bars: int = 30) -> pd.DataFrame:
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=n_bars, freq="D")
    df = pd.DataFrame(
        {
            "open": [close_price * 0.99] * n_bars,
            "high": [close_price * 1.01] * n_bars,
            "low": [close_price * 0.98] * n_bars,
            "close": [close_price] * n_bars,
            "volume": [volume] * n_bars,
        },
        index=dates,
    )
    return df


def test_universe_eligibility_included():
    calendar = MarketCalendarGateService()
    constructor = DynamicUniverseConstructor(
        calendar_service=calendar,
        min_price=15.0,
        min_addv_usd=25_000_000.0,
        max_spread_bps=6.0,
    )

    # AAPL: price $150, volume 2M -> ADDV20 = $300M (> $25M), spread 2 bps
    df = _create_synthetic_bars(close_price=150.0, volume=2_000_000)
    quote = ExecutionQuote(
        ticker="AAPL",
        bid=149.98,
        ask=150.01,
        timestamp=datetime.now(timezone.utc),
    )

    # Normal Wednesday during market hours
    as_of = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)
    rec = constructor.evaluate_symbol("AAPL", df, quote=quote, as_of=as_of)

    assert rec.is_eligible is True
    assert "ELIGIBLE" in rec.reason
    assert rec.price == 150.0
    assert rec.addv_20_usd >= 25_000_000.0
    assert rec.spread_bps <= 6.0
    assert rec.is_tradable is True
    assert rec.is_halted is False


def test_universe_eligibility_excluded_low_price():
    constructor = DynamicUniverseConstructor(min_price=15.0)
    # Penny stock $5.00
    df = _create_synthetic_bars(close_price=5.0, volume=10_000_000)
    as_of = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)
    rec = constructor.evaluate_symbol("PENNY", df, as_of=as_of)

    assert rec.is_eligible is False
    assert "Price $5.00 < $15.00 min" in rec.reason


def test_universe_eligibility_excluded_low_liquidity():
    constructor = DynamicUniverseConstructor(min_price=15.0, min_addv_usd=25_000_000.0)
    # $20 price, 50k volume -> ADDV20 = $1M (< $25M)
    df = _create_synthetic_bars(close_price=20.0, volume=50_000)
    as_of = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)
    rec = constructor.evaluate_symbol("ILLIQ", df, as_of=as_of)

    assert rec.is_eligible is False
    assert "ADDV20" in rec.reason


def test_universe_eligibility_excluded_wide_spread():
    constructor = DynamicUniverseConstructor(max_spread_bps=6.0)
    df = _create_synthetic_bars(close_price=50.0, volume=1_000_000)
    # Spread 20 bps
    quote = ExecutionQuote(
        ticker="WIDE",
        bid=49.95,
        ask=50.05,
        timestamp=datetime.now(timezone.utc),
    )
    as_of = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)
    rec = constructor.evaluate_symbol("WIDE", df, quote=quote, as_of=as_of)

    assert rec.is_eligible is False
    assert "Spread" in rec.reason


def test_universe_eligibility_excluded_halted_and_blackout():
    calendar = MarketCalendarGateService()
    calendar.register_halt("HALTCO", "Regulatory investigation")
    # Blackout within 7 days
    as_of = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)
    calendar.register_corporate_action("EARNCO", "EARNINGS", datetime(2026, 6, 12, tzinfo=timezone.utc))

    constructor = DynamicUniverseConstructor(calendar_service=calendar)

    df = _create_synthetic_bars(close_price=100.0, volume=2_000_000)
    rec_halt = constructor.evaluate_symbol("HALTCO", df, as_of=as_of)
    assert rec_halt.is_eligible is False
    assert rec_halt.is_halted is True
    assert "halted" in rec_halt.reason.lower()

    rec_earn = constructor.evaluate_symbol("EARNCO", df, as_of=as_of)
    assert rec_earn.is_eligible is False
    assert rec_earn.has_corporate_action_blackout is True
    assert "blackout" in rec_earn.reason.lower()


def test_dynamic_universe_versioning(tmp_path):
    db_path = str(tmp_path / "test_univ.db")
    db = Database(db_path)
    constructor = DynamicUniverseConstructor(
        min_price=15.0,
        min_addv_usd=25_000_000.0,
        db=db,
    )

    as_of = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)
    pool = {
        "AAPL": _create_synthetic_bars(150.0, 2_000_000),
        "MSFT": _create_synthetic_bars(300.0, 1_000_000),
        "PENNY": _create_synthetic_bars(2.0, 100_000),
    }

    quotes = {
        "AAPL": ExecutionQuote(ticker="AAPL", bid=149.99, ask=150.01, timestamp=as_of),
        "MSFT": ExecutionQuote(ticker="MSFT", bid=299.98, ask=300.02, timestamp=as_of),
        "PENNY": ExecutionQuote(ticker="PENNY", bid=1.99, ask=2.01, timestamp=as_of),
    }

    univ_ver = constructor.construct_universe(
        candidate_pool=pool,
        quotes=quotes,
        as_of=as_of,
        run_id="run_test_01",
    )

    assert isinstance(univ_ver, DynamicUniverseVersion)
    assert univ_ver.total_scanned == 3
    assert univ_ver.eligible_symbols == ["AAPL", "MSFT"]
    assert "PENNY" not in univ_ver.eligible_symbols
    assert univ_ver.universe_version.startswith("univ_20260610_")

    # Check serialization
    d = univ_ver.to_dict()
    assert d["total_scanned"] == 3
    assert "AAPL" in d["eligibility_records"]
    j = univ_ver.to_json()
    assert "univ_20260610_" in j
