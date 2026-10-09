"""
Unit tests for Market Calendar, Session Status, Symbol Halts, and Corporate Action Gates.
Validates ADR 0002 fail-closed semantics for trading entry gates.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import pytest

from src.data.calendar import MarketCalendarGateService, get_nyse_holidays
from src.domain.models import MarketSessionStatus, StrategyType
from src.screener.screener import QuantitativeScreener

pytestmark = pytest.mark.unit

EASTERN = ZoneInfo("America/New_York")


def test_market_open_regular_hours():
    service = MarketCalendarGateService()
    # Wednesday Oct 7, 2026 at 11:00 AM Eastern
    dt_et = datetime(2026, 10, 7, 11, 0, 0, tzinfo=EASTERN)
    dt_utc = dt_et.astimezone(timezone.utc)

    status = service.get_session_status(dt_utc)
    assert status == MarketSessionStatus.OPEN

    result = service.evaluate_entry_gate("AAPL", as_of=dt_utc)
    assert result.passed is True
    assert result.session_status == MarketSessionStatus.OPEN
    assert "All entry gates passed" in result.reason


def test_market_closed_weekend():
    service = MarketCalendarGateService()
    # Saturday Oct 10, 2026 at 12:00 PM Eastern
    dt_sat = datetime(2026, 10, 10, 12, 0, 0, tzinfo=EASTERN)
    assert service.get_session_status(dt_sat) == MarketSessionStatus.CLOSED

    # Sunday Oct 11, 2026 at 12:00 PM Eastern
    dt_sun = datetime(2026, 10, 11, 12, 0, 0, tzinfo=EASTERN)
    assert service.get_session_status(dt_sun) == MarketSessionStatus.CLOSED

    result = service.evaluate_entry_gate("AAPL", as_of=dt_sat)
    assert result.passed is False
    assert result.session_status == MarketSessionStatus.CLOSED
    assert "CLOSED" in result.reason


def test_market_pre_market_and_after_hours():
    service = MarketCalendarGateService()
    # Wednesday Oct 7, 2026 at 8:00 AM Eastern (Pre-market)
    dt_pre = datetime(2026, 10, 7, 8, 0, 0, tzinfo=EASTERN)
    assert service.get_session_status(dt_pre) == MarketSessionStatus.PRE_MARKET

    res_pre = service.evaluate_entry_gate("AAPL", as_of=dt_pre)
    assert res_pre.passed is False
    assert res_pre.session_status == MarketSessionStatus.PRE_MARKET

    # Wednesday Oct 7, 2026 at 5:00 PM Eastern (After-hours)
    dt_post = datetime(2026, 10, 7, 17, 0, 0, tzinfo=EASTERN)
    assert service.get_session_status(dt_post) == MarketSessionStatus.AFTER_HOURS

    res_post = service.evaluate_entry_gate("AAPL", as_of=dt_post)
    assert res_post.passed is False
    assert res_post.session_status == MarketSessionStatus.AFTER_HOURS


def test_market_closed_holidays():
    service = MarketCalendarGateService()
    # Thanksgiving 2026 (Thursday, Nov 26, 2026)
    dt_thanksgiving = datetime(2026, 11, 26, 11, 0, 0, tzinfo=EASTERN)
    assert service.get_session_status(dt_thanksgiving) == MarketSessionStatus.CLOSED

    # Christmas 2026 (Friday, Dec 25, 2026)
    dt_christmas = datetime(2026, 12, 25, 11, 0, 0, tzinfo=EASTERN)
    assert service.get_session_status(dt_christmas) == MarketSessionStatus.CLOSED

    res = service.evaluate_entry_gate("AAPL", as_of=dt_christmas)
    assert res.passed is False
    assert res.session_status == MarketSessionStatus.CLOSED


def test_nyse_holidays_calendar():
    holidays_2026 = get_nyse_holidays(2026)
    # Check key holidays exist in set
    assert any(d.month == 1 and d.day == 1 for d in holidays_2026)   # New Year's Day
    assert any(d.month == 11 for d in holidays_2026)                  # Thanksgiving
    assert any(d.month == 12 and d.day == 25 for d in holidays_2026) # Christmas


def test_market_session_unknown_fallback():
    service = MarketCalendarGateService()

    class BadDatetime:
        """Triggers exception when astimezone is called."""
        def astimezone(self, tz):
            raise ValueError("Corrupt timezone data")

    status = service.get_session_status(BadDatetime())
    assert status == MarketSessionStatus.UNKNOWN

    res = service.evaluate_entry_gate("AAPL", as_of=BadDatetime())
    assert res.passed is False
    assert res.session_status == MarketSessionStatus.UNKNOWN
    assert "UNKNOWN" in res.reason


def test_symbol_trading_halt():
    service = MarketCalendarGateService()
    # Regular trading hours
    dt = datetime(2026, 10, 7, 11, 0, 0, tzinfo=EASTERN)

    service.register_halt("GME", "Pending news announcement (Code LUDP)")
    assert service.is_symbol_halted("GME")[0] is True

    res = service.evaluate_entry_gate("GME", as_of=dt)
    assert res.passed is False
    assert res.session_status == MarketSessionStatus.HALTED
    assert "Code LUDP" in res.reason

    # Non-halted symbol should pass
    res_aapl = service.evaluate_entry_gate("AAPL", as_of=dt)
    assert res_aapl.passed is True

    # Clear halt
    service.clear_halt("GME")
    assert service.is_symbol_halted("GME")[0] is False
    res_after = service.evaluate_entry_gate("GME", as_of=dt)
    assert res_after.passed is True


def test_corporate_action_and_earnings_blackout():
    service = MarketCalendarGateService(blackout_threshold_days=7)
    # Today is Oct 7, 2026
    dt_today = datetime(2026, 10, 7, 11, 0, 0, tzinfo=EASTERN)

    # Earnings in 3 days (Oct 10, 2026) -> Within 7d blackout
    service.register_corporate_action("TSLA", "EARNINGS_RELEASE", datetime(2026, 10, 10, tzinfo=EASTERN))
    # Stock split in 14 days (Oct 21, 2026) -> Outside 7d blackout
    service.register_corporate_action("MSFT", "FORWARD_SPLIT_3_FOR_1", datetime(2026, 10, 21, tzinfo=EASTERN))

    res_tsla = service.evaluate_entry_gate("TSLA", as_of=dt_today)
    assert res_tsla.passed is False
    assert res_tsla.gate_name == "CORPORATE_ACTION_GATE"
    assert "EARNINGS_RELEASE" in res_tsla.reason
    assert "3d away" in res_tsla.reason

    res_msft = service.evaluate_entry_gate("MSFT", as_of=dt_today)
    assert res_msft.passed is True

    # Clear corporate actions
    service.clear_corporate_actions("TSLA")
    res_tsla_cleared = service.evaluate_entry_gate("TSLA", as_of=dt_today)
    assert res_tsla_cleared.passed is True


def test_screener_integration_with_calendar_gate():
    service = MarketCalendarGateService()
    # Wednesday 11:00 AM ET (market open)
    as_of = datetime(2026, 10, 7, 11, 0, 0, tzinfo=EASTERN)

    # Halt NVDA and put TSLA in earnings blackout
    service.register_halt("NVDA", "Volatility trading pause")
    service.register_corporate_action("TSLA", "EARNINGS", datetime(2026, 10, 9, tzinfo=EASTERN))

    screener = QuantitativeScreener(calendar_gate=service)

    # Synthetic candidate dataframes
    from tests.test_screener import generate_synthetic_bars
    from src.data.pipeline import MarketDataPipeline

    spy_df = MarketDataPipeline.compute_indicators(generate_synthetic_bars(220, base_price=450.0))
    aapl_df = MarketDataPipeline.compute_indicators(generate_synthetic_bars(100, base_price=150.0))
    nvda_df = MarketDataPipeline.compute_indicators(generate_synthetic_bars(100, base_price=120.0))
    tsla_df = MarketDataPipeline.compute_indicators(generate_synthetic_bars(100, base_price=200.0))

    universe = {
        "SPY": spy_df,
        "AAPL": aapl_df,
        "NVDA": nvda_df,
        "TSLA": tsla_df,
    }

    # NVDA is halted -> should be skipped
    # TSLA has earnings blackout -> should be skipped
    # Only AAPL should even be evaluated
    candidates = screener.scan_universe(universe, spy_df=spy_df, as_of=as_of)
    scanned_tickers = [c.ticker for c in candidates]
    assert "NVDA" not in scanned_tickers
    assert "TSLA" not in scanned_tickers
