"""
Tests for Deterministic Exit Policy: Time Stops, Trailing Stops, Pre-Earnings, and Gaps (P3-05).
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import pytest

from src.broker.simulated import SimulatedPaperBroker
from src.domain.models import ExitReason, OrderSide, Position, PositionStatus
from src.risk.exit_policy import ExitPolicyConfig, ExitPolicyManager
from src.risk.engine import RiskEngine
from src.storage.db import Database


@pytest.fixture
def exit_env():
    fd_db, path_db = tempfile.mkstemp(suffix=".db")
    os.close(fd_db)
    db = Database(db_path=path_db)
    db.record_reconciliation_event("evt_clean", "h_local", "h_broker", "[]", "HEALTHY_MATCH")

    fd_lock, path_lock = tempfile.mkstemp(suffix=".lock")
    os.close(fd_lock)
    os.remove(path_lock)
    lock_path = Path(path_lock)

    broker = SimulatedPaperBroker(initial_cash=100.0)
    config = ExitPolicyConfig(
        max_holding_days=10,
        trailing_stop_activation_pct=0.05,
        trailing_stop_distance_pct=0.03,
        pre_earnings_exit_days=1.0,
    )
    manager = ExitPolicyManager(db=db, broker=broker, config=config)
    engine = RiskEngine(
        db=db,
        broker=broker,
        lock_file=lock_path,
        exit_policy_config=config,
    )

    yield manager, engine, broker, db

    if os.path.exists(path_db):
        os.remove(path_db)
    if lock_path.exists():
        lock_path.unlink()


def _create_open_position(
    broker: SimulatedPaperBroker,
    db: Database,
    ticker: str,
    entry_price: float = 100.0,
    stop_loss: float = 95.0,
    take_profit: float = 110.0,
    opened_at: Optional[datetime] = None,
) -> Position:
    now = opened_at or datetime.now(timezone.utc)
    pos_id = f"pos_{ticker}"
    pos = Position(
        position_id=pos_id,
        ticker=ticker,
        side=OrderSide.BUY,
        qty=1.0,
        entry_price=entry_price,
        current_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        market_value=entry_price,
        unrealized_pnl=0.0,
        status=PositionStatus.OPEN,
        opened_at=now,
    )
    broker.positions[pos_id] = pos
    db.save_position(pos)
    return pos


def test_time_stop_triggers_after_max_holding_period(exit_env):
    manager, _, broker, db = exit_env
    now = datetime.now(timezone.utc)

    # 1. Position held 9 days (< 10) -> no time stop
    pos1 = _create_open_position(broker, db, "AAPL", opened_at=now - timedelta(days=9))
    should_exit, reason, price, _ = manager.evaluate_position_exit(pos1, current_price=101.0, as_of=now)
    assert should_exit is False
    assert reason is None

    # 2. Position held 10.5 days (>= 10) -> time stop triggers!
    pos2 = _create_open_position(broker, db, "MSFT", opened_at=now - timedelta(days=10, hours=12))
    should_exit, reason, price, expl = manager.evaluate_position_exit(pos2, current_price=101.0, as_of=now)
    assert should_exit is True
    assert reason == ExitReason.TIME_STOP
    assert "holding duration" in expl


def test_trailing_stop_activation_and_execution(exit_env):
    manager, _, broker, db = exit_env

    # Entry at 100.0
    pos = _create_open_position(broker, db, "NVDA", entry_price=100.0, stop_loss=95.0, take_profit=130.0)

    # 1. Price moves to 104.0 (+4% gain) -> below 5% activation threshold
    should_exit, reason, _, _ = manager.evaluate_position_exit(pos, current_price=104.0)
    assert should_exit is False

    # 2. Price surges to 110.0 (+10% gain) -> activates trailing stop (trail level = 110 * 0.97 = 106.70)
    should_exit, reason, _, _ = manager.evaluate_position_exit(pos, current_price=110.0)
    assert should_exit is False
    assert manager.high_water_marks[pos.position_id] == 110.0

    # 3. Price pulls back to 108.0 (> 106.70) -> still holding
    should_exit, reason, _, _ = manager.evaluate_position_exit(pos, current_price=108.0)
    assert should_exit is False

    # 4. Price pulls back to 106.50 (<= 106.70 trail level) -> trailing stop triggers!
    should_exit, reason, exit_price, expl = manager.evaluate_position_exit(pos, current_price=106.50)
    assert should_exit is True
    assert reason == ExitReason.TRAILING_STOP
    assert "Trailing stop triggered" in expl


def test_pre_earnings_mandatory_liquidation(exit_env):
    manager, _, broker, db = exit_env
    now = datetime.now(timezone.utc)

    pos = _create_open_position(broker, db, "TSLA", entry_price=100.0)

    # 1. Earnings in 3 days -> not yet within 1 day (24h) threshold
    db.save_instrument_metadata("TSLA", "NASDAQ", "v1", next_earnings_date=now + timedelta(days=3))
    should_exit, reason, _, _ = manager.evaluate_position_exit(pos, current_price=102.0, as_of=now)
    assert should_exit is False

    # 2. Earnings in 12 hours -> within 1 day -> mandatory exit!
    db.save_instrument_metadata("TSLA", "NASDAQ", "v1", next_earnings_date=now + timedelta(hours=12))
    should_exit, reason, _, expl = manager.evaluate_position_exit(pos, current_price=102.0, as_of=now)
    assert should_exit is True
    assert reason == ExitReason.EARNINGS_PRE_EXIT
    assert "earnings" in expl.lower()


def test_overnight_gap_through_stop_loss(exit_env):
    manager, _, broker, db = exit_env

    # Entry at 100.0, Stop Loss at 95.0
    pos = _create_open_position(broker, db, "AMZN", entry_price=100.0, stop_loss=95.0)

    # Overnight gap down: open at 90.0 (< 95.0)
    should_exit, reason, exit_price, expl = manager.evaluate_position_exit(pos, current_price=90.0)
    assert should_exit is True
    assert reason == ExitReason.GAP_STOP
    assert exit_price == 90.0
    assert "gapped below SL" in expl


def test_missing_or_invalid_quote_handling(exit_env):
    manager, _, broker, db = exit_env

    pos = _create_open_position(broker, db, "GOOGL", entry_price=150.0)

    # Missing quote (None)
    should_exit, reason, _, expl = manager.evaluate_position_exit(pos, current_price=None)
    assert should_exit is False
    assert reason is None
    assert "Missing or invalid" in expl

    # Invalid quote (<= 0)
    should_exit, reason, _, expl = manager.evaluate_position_exit(pos, current_price=-5.0)
    assert should_exit is False
    assert reason is None


def test_exit_policy_execution_and_db_persistence(exit_env):
    manager, engine, broker, db = exit_env

    # Setup open position
    pos = _create_open_position(broker, db, "META", entry_price=200.0, stop_loss=190.0, take_profit=230.0)

    # Run exit policy via RiskEngine.run_bracket_watchdog with take-profit price 235.0
    exits = engine.run_bracket_watchdog({"META": 235.0})
    assert len(exits) == 1
    ticker, exit_reason, exit_price = exits[0]
    assert ticker == "META"
    assert exit_reason == ExitReason.TAKE_PROFIT.value
    assert exit_price == 235.0

    # Verify position is closed in DB
    db_pos = db.get_position(pos.position_id)
    assert db_pos is not None
    assert db_pos.status == PositionStatus.CLOSED
    assert db_pos.exit_reason == ExitReason.TAKE_PROFIT
    assert db_pos.realized_pnl == 35.0

    # Verify audit log in DB
    logs = db.get_audit_logs(component="ExitPolicy")
    assert len(logs) > 0
    assert any("EXIT_EXECUTED" in (l.event_name if hasattr(l, "event_name") else l["event_name"]) for l in logs)
