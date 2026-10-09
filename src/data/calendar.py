"""
Market Calendar and Entry Gate Service.
Enforces fail-closed entry gates:
1. US regular market trading hours (9:30 AM - 4:00 PM Eastern, Monday-Friday, excluding NYSE holidays).
2. Active trading halts per symbol.
3. Corporate action and earnings announcement blackout windows (< 7 days).
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from src.domain.models import GateCheckResult, MarketSessionStatus

EASTERN_TZ = ZoneInfo("America/New_York")


def get_easter_date(year: int) -> date:
    """Computes Easter Sunday using the Anonymous Gregorian algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def get_nyse_holidays(year: int) -> Set[date]:
    """
    Returns standard observed NYSE holiday dates for a given year:
    - New Year's Day (Jan 1, observed)
    - Martin Luther King Jr. Day (3rd Monday in Jan)
    - Washington's Birthday (3rd Monday in Feb)
    - Good Friday (Friday before Easter)
    - Memorial Day (Last Monday in May)
    - Juneteenth (June 19, observed, instituted 2021)
    - Independence Day (July 4, observed)
    - Labor Day (1st Monday in Sep)
    - Thanksgiving Day (4th Thursday in Nov)
    - Christmas Day (Dec 25, observed)
    """
    holidays: Set[date] = set()

    def observe(d: date) -> date:
        # If Saturday, observed preceding Friday. If Sunday, observed following Monday.
        if d.weekday() == 5:
            return d - timedelta(days=1)
        elif d.weekday() == 6:
            return d + timedelta(days=1)
        return d

    # 1. New Year's Day
    holidays.add(observe(date(year, 1, 1)))

    # 2. Martin Luther King Jr. Day (3rd Monday in January)
    d = date(year, 1, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    holidays.add(d + timedelta(weeks=2))

    # 3. Washington's Birthday (3rd Monday in February)
    d = date(year, 2, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    holidays.add(d + timedelta(weeks=2))

    # 4. Good Friday
    easter = get_easter_date(year)
    holidays.add(easter - timedelta(days=2))

    # 5. Memorial Day (Last Monday in May)
    d = date(year, 5, 31)
    while d.weekday() != 0:
        d -= timedelta(days=1)
    holidays.add(d)

    # 6. Juneteenth (June 19, observed)
    if year >= 2021:
        holidays.add(observe(date(year, 6, 19)))

    # 7. Independence Day (July 4, observed)
    holidays.add(observe(date(year, 7, 4)))

    # 8. Labor Day (1st Monday in September)
    d = date(year, 9, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    holidays.add(d)

    # 9. Thanksgiving Day (4th Thursday in November)
    d = date(year, 11, 1)
    while d.weekday() != 3:
        d += timedelta(days=1)
    holidays.add(d + timedelta(weeks=3))

    # 10. Christmas Day (Dec 25, observed)
    holidays.add(observe(date(year, 12, 25)))

    return holidays


class MarketCalendarGateService:
    def __init__(self, blackout_threshold_days: int = 7):
        self.blackout_threshold_days = blackout_threshold_days
        self._halted_symbols: Dict[str, str] = {}  # ticker -> reason
        self._corporate_actions: Dict[str, List[Tuple[str, datetime]]] = {}  # ticker -> list of (action_type, ex_date)

    def register_halt(self, ticker: str, reason: str = "Trading halted by exchange") -> None:
        """Registers a trading halt for a symbol."""
        self._halted_symbols[ticker.upper()] = reason

    def clear_halt(self, ticker: str) -> None:
        """Clears a trading halt for a symbol."""
        self._halted_symbols.pop(ticker.upper(), None)

    def is_symbol_halted(self, ticker: str) -> Tuple[bool, Optional[str]]:
        """Checks if a symbol is actively halted."""
        ticker = ticker.upper()
        if ticker in self._halted_symbols:
            return True, self._halted_symbols[ticker]
        return False, None

    def register_corporate_action(self, ticker: str, action_type: str, ex_date: datetime) -> None:
        """Registers an upcoming corporate action or earnings date."""
        ticker = ticker.upper()
        if ex_date.tzinfo is None:
            ex_date = ex_date.replace(tzinfo=timezone.utc)
        self._corporate_actions.setdefault(ticker, []).append((action_type, ex_date))

    def clear_corporate_actions(self, ticker: Optional[str] = None) -> None:
        """Clears corporate actions for a ticker, or all if ticker is None."""
        if ticker:
            self._corporate_actions.pop(ticker.upper(), None)
        else:
            self._corporate_actions.clear()

    def has_corporate_action_blackout(
        self, ticker: str, as_of: datetime, threshold_days: Optional[int] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Checks if symbol is within the corporate action / earnings blackout window.
        Returns (is_blacked_out, reason_description).
        """
        ticker = ticker.upper()
        actions = self._corporate_actions.get(ticker, [])
        if not actions:
            return False, None

        threshold = threshold_days if threshold_days is not None else self.blackout_threshold_days
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)

        for action_type, act_date in actions:
            diff_days = abs((act_date.date() - as_of.date()).days)
            if diff_days <= threshold:
                return (
                    True,
                    f"{action_type} scheduled for {act_date.strftime('%Y-%m-%d')} ({diff_days}d away <= {threshold}d blackout threshold)",
                )

        return False, None

    def get_session_status(self, dt: Optional[datetime] = None) -> MarketSessionStatus:
        """
        Evaluates US equity market session status at datetime `dt`.
        Adheres to fail-closed semantics.
        """
        if dt is None:
            dt = datetime.now(timezone.utc)

        try:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            # Convert to Eastern Time
            et_dt = dt.astimezone(EASTERN_TZ)
            current_date = et_dt.date()
            current_time = et_dt.time()

            # 1. Check weekends (Saturday=5, Sunday=6)
            if et_dt.weekday() in (5, 6):
                return MarketSessionStatus.CLOSED

            # 2. Check NYSE Holidays
            holidays = get_nyse_holidays(current_date.year)
            if current_date in holidays:
                return MarketSessionStatus.CLOSED

            # 3. Check regular trading hours (09:30:00 to 16:00:00 ET)
            market_open = dtime(9, 30, 0)
            market_close = dtime(16, 0, 0)
            pre_market_open = dtime(4, 0, 0)
            post_market_close = dtime(20, 0, 0)

            if market_open <= current_time < market_close:
                return MarketSessionStatus.OPEN
            elif pre_market_open <= current_time < market_open:
                return MarketSessionStatus.PRE_MARKET
            elif market_close <= current_time < post_market_close:
                return MarketSessionStatus.AFTER_HOURS
            else:
                return MarketSessionStatus.CLOSED

        except Exception:
            # Fail closed: Any error evaluating timezone or calendar returns UNKNOWN
            return MarketSessionStatus.UNKNOWN

    def evaluate_entry_gate(
        self, ticker: str, as_of: Optional[datetime] = None
    ) -> GateCheckResult:
        """
        Evaluates all entry gates for a ticker:
        1. Market Session must be OPEN.
        2. Symbol must NOT be halted.
        3. Symbol must NOT have an active corporate action / earnings blackout.
        Fails closed on any failure or unknown state.
        """
        ticker = ticker.upper()
        checked_time = as_of if isinstance(as_of, datetime) else datetime.now(timezone.utc)
        session_status = self.get_session_status(as_of)

        # Gate 1: Session Status Check
        if session_status != MarketSessionStatus.OPEN:
            return GateCheckResult(
                passed=False,
                gate_name="MARKET_SESSION_GATE",
                session_status=session_status,
                reason=f"Market session is {session_status.value}; new entries prohibited outside regular hours (ADR 0002)",
                ticker=ticker,
                timestamp=checked_time,
            )

        # Gate 2: Trading Halt Check
        is_halted, halt_reason = self.is_symbol_halted(ticker)
        if is_halted:
            return GateCheckResult(
                passed=False,
                gate_name="SYMBOL_HALT_GATE",
                session_status=MarketSessionStatus.HALTED,
                reason=f"Symbol {ticker} is HALTED: {halt_reason}",
                ticker=ticker,
                timestamp=as_of,
            )

        # Gate 3: Corporate Action / Earnings Blackout Check
        is_blacked_out, blackout_reason = self.has_corporate_action_blackout(ticker, as_of)
        if is_blacked_out:
            return GateCheckResult(
                passed=False,
                gate_name="CORPORATE_ACTION_GATE",
                session_status=session_status,
                reason=f"Corporate action / earnings blackout for {ticker}: {blackout_reason}",
                ticker=ticker,
                timestamp=as_of,
            )

        # All Gates Passed
        return GateCheckResult(
            passed=True,
            gate_name="ENTRY_GATE_ALL_PASS",
            session_status=MarketSessionStatus.OPEN,
            reason="All entry gates passed: regular market hours, unhalted, no corporate action risk",
            ticker=ticker,
            timestamp=as_of,
        )
