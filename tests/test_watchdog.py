"""
Tests for Watchdog Cadence and Degraded Protection Policy (P3-02).
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from src.broker.simulated import SimulatedPaperBroker
from src.domain.models import (
    CandidateStatus,
    OrderSide,
    ProtectionMode,
    ScreenedCandidate,
    StrategyType,
)
from src.risk.engine import RiskEngine
from src.storage.db import Database


@pytest.fixture
def watchdog_env():
    fd_db, path_db = tempfile.mkstemp(suffix=".db")
    os.close(fd_db)
    db = Database(db_path=path_db)
    # Record clean reconciliation event so recon gate passes
    db.record_reconciliation_event("evt_clean", "h_local", "h_broker", "[]", "HEALTHY_MATCH")

    fd_lock, path_lock = tempfile.mkstemp(suffix=".lock")
    os.close(fd_lock)
    os.remove(path_lock)
    lock_path = Path(path_lock)

    broker = SimulatedPaperBroker(initial_cash=100.0)
    engine = RiskEngine(db=db, broker=broker, lock_file=lock_path, watchdog_max_cadence_seconds=60.0)

    yield engine, broker, db, lock_path

    if os.path.exists(path_db):
        os.remove(path_db)
    if lock_path.exists():
        lock_path.unlink()


def _make_candidate(ticker: str) -> ScreenedCandidate:
    return ScreenedCandidate(
        candidate_id=f"cand_{ticker}",
        timestamp=datetime.now(timezone.utc),
        ticker=ticker,
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        risk_r=5.0,
        allocated_usd=25.0,
        rank_score=1.0,
        status=CandidateStatus.APPROVED,
    )


def test_native_bracket_broker_allows_entry_without_watchdog_run(watchdog_env):
    engine, broker, db, _ = watchdog_env
    cand = _make_candidate("AAPL")
    db.save_candidate(cand)

    assert broker.supports_native_bracket is True
    assert engine.is_watchdog_healthy() is False  # hasn't run yet

    ok, order, res, msg = engine.validate_and_route_order(cand)
    assert ok is True
    assert "successfully routed" in msg


def test_unsupported_broker_blocked_when_watchdog_never_ran(watchdog_env):
    engine, broker, db, _ = watchdog_env

    class MockNoBracketBroker(SimulatedPaperBroker):
        @property
        def supports_native_bracket(self) -> bool:
            return False

    no_bracket_broker = MockNoBracketBroker(initial_cash=100.0)
    engine.broker = no_bracket_broker

    cand = _make_candidate("MSFT")
    db.save_candidate(cand)

    ok, order, res, msg = engine.validate_and_route_order(cand)
    assert ok is False
    assert "lacks native bracket protection and software watchdog is unhealthy" in msg


def test_unsupported_broker_allowed_when_watchdog_healthy(watchdog_env):
    engine, broker, db, _ = watchdog_env

    class MockNoBracketBroker(SimulatedPaperBroker):
        @property
        def supports_native_bracket(self) -> bool:
            return False

    no_bracket_broker = MockNoBracketBroker(initial_cash=100.0)
    engine.broker = no_bracket_broker

    # Run bracket watchdog to generate healthy heartbeat
    engine.run_bracket_watchdog({})
    assert engine.is_watchdog_healthy() is True

    cand = _make_candidate("GOOGL")
    db.save_candidate(cand)

    ok, order, res, msg = engine.validate_and_route_order(cand)
    assert ok is True


def test_unsupported_broker_blocked_when_watchdog_cadence_exceeded(watchdog_env):
    engine, broker, db, _ = watchdog_env

    class MockNoBracketBroker(SimulatedPaperBroker):
        @property
        def supports_native_bracket(self) -> bool:
            return False

    no_bracket_broker = MockNoBracketBroker(initial_cash=100.0)
    engine.broker = no_bracket_broker

    # Set last watchdog run in the past (70s ago, threshold is 60s)
    past_time = datetime.now(timezone.utc) - timedelta(seconds=70)
    engine.last_watchdog_run_at = past_time
    assert engine.is_watchdog_healthy() is False

    cand = _make_candidate("NVDA")
    db.save_candidate(cand)

    ok, order, res, msg = engine.validate_and_route_order(cand)
    assert ok is False
    assert "watchdog is unhealthy or exceeds cadence" in msg


def test_active_degraded_unprotected_position_blocks_new_entries(watchdog_env):
    engine, broker, db, _ = watchdog_env

    # Route initial order
    cand1 = _make_candidate("TSLA")
    db.save_candidate(cand1)
    ok, order, res, msg = engine.validate_and_route_order(cand1)
    assert ok is True

    pos = broker.get_position("TSLA")
    assert pos is not None

    # Manually mark TSLA protection as DEGRADED_UNPROTECTED in DB
    db.save_protection_status(
        protection_id="prot_tsla",
        position_id=pos.position_id,
        ticker="TSLA",
        protection_mode=ProtectionMode.DEGRADED_UNPROTECTED.value,
        watchdog_healthy=0,
        degradation_reason="Manual test degradation",
    )

    # Next candidate must be blocked
    cand2 = _make_candidate("AMZN")
    db.save_candidate(cand2)
    ok2, order2, res2, msg2 = engine.validate_and_route_order(cand2)
    assert ok2 is False
    assert "DEGRADED_UNPROTECTED state" in msg2
