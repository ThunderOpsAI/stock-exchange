"""
Unit tests for Deterministic Risk Engine.
Validates position sizing, $3.00 risk cap, 3-slot contention, 10% buffer,
Two-Tier Circuit Breaker ($80 soft, $70 hard + lock file), and bracket watchdog.
"""

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import pytest

pytestmark = pytest.mark.unit

from src.broker.simulated import SimulatedPaperBroker
from src.domain.models import (
    CandidateStatus,
    CircuitBreakerTier,
    OrderState,
    PositionStatus,
    ScreenedCandidate,
    StrategyType,
)
from src.risk.engine import RiskEngine
from src.storage.db import Database


@pytest.fixture
def risk_env():
    fd_db, path_db = tempfile.mkstemp(suffix=".db")
    os.close(fd_db)
    db = Database(db_path=path_db)

    fd_lock, path_lock = tempfile.mkstemp(suffix=".lock")
    os.close(fd_lock)
    os.remove(path_lock)  # start unlocked
    lock_path = Path(path_lock)

    broker = SimulatedPaperBroker(initial_cash=100.0)
    engine = RiskEngine(db=db, broker=broker, lock_file=lock_path)
    db.record_reconciliation_event("evt_clean", "h_local", "h_broker", "[]", "HEALTHY_MATCH")

    yield engine, broker, db, lock_path

    if os.path.exists(path_db):
        os.remove(path_db)
    if lock_path.exists():
        lock_path.unlink()


def test_position_sizing_and_risk_cap(risk_env):
    engine, broker, db, _ = risk_env
    now = datetime.now(timezone.utc)

    # 1. Normal candidate: entry 100, SL 95 (risk $5/share).
    # $3.00 risk cap formula: ($3.00 * 100) / 5 = $60 -> capped at slot $30.00.
    cand = ScreenedCandidate(
        candidate_id="c_size_1",
        timestamp=now,
        ticker="AAPL",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        risk_r=5.0,
        allocated_usd=30.0,
        rank_score=1.5,
    )
    ok, allocated, qty, reason = engine.calculate_position_size(cand, current_cash=100.0)
    assert ok is True
    assert allocated == 30.0
    assert qty == 0.30

    # 2. High-volatility candidate: entry 100, SL 80 (risk $20/share).
    # ($3.00 * 100) / 20 = $15.00 -> should scale down allocation from $30 to $15!
    cand_high_vol = ScreenedCandidate(
        candidate_id="c_size_2",
        timestamp=now,
        ticker="VOL",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=100.0,
        stop_loss=80.0,
        take_profit=140.0,
        risk_r=20.0,
        allocated_usd=30.0,
        rank_score=1.5,
    )
    ok2, allocated2, qty2, _ = engine.calculate_position_size(cand_high_vol, current_cash=100.0)
    assert ok2 is True
    assert allocated2 == 15.0
    assert qty2 == 0.15
    # Risk check: 0.15 * $20 = $3.00!
    assert round(qty2 * cand_high_vol.risk_r, 2) == 3.00

    # 3. Cash buffer check: if cash is $18.00, available cash is $8.00 < $10.00 minimum
    ok3, _, _, reason3 = engine.calculate_position_size(cand, current_cash=18.0)
    assert ok3 is False
    assert "buffer" in reason3.lower()


def test_slot_capacity_and_routing(risk_env):
    engine, broker, db, _ = risk_env
    now = datetime.now(timezone.utc)
    broker.set_price("SYM1", 100.0)
    broker.set_price("SYM2", 100.0)
    broker.set_price("SYM3", 100.0)
    broker.set_price("SYM4", 100.0)

    # Route 3 orders to occupy 3 slots
    for i, sym in enumerate(["SYM1", "SYM2", "SYM3"], 1):
        cand = ScreenedCandidate(
            candidate_id=f"c_{sym}",
            timestamp=now,
            ticker=sym,
            strategy=StrategyType.TREND_PULLBACK,
            entry_est=100.0,
            stop_loss=90.0,
            take_profit=120.0,
            risk_r=10.0,
            allocated_usd=30.0,
            rank_score=1.0,
        )
        db.save_candidate(cand)
        ok, order, res, msg = engine.validate_and_route_order(cand)
        assert ok is True
        assert order.state == OrderState.FILLED

    assert len(broker.get_positions()) == 3

    # Attempt 4th position -> must be blocked
    cand4 = ScreenedCandidate(
        candidate_id="c_SYM4",
        timestamp=now,
        ticker="SYM4",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=100.0,
        stop_loss=90.0,
        take_profit=120.0,
        risk_r=10.0,
        allocated_usd=30.0,
        rank_score=1.0,
    )
    db.save_candidate(cand4)
    ok4, _, _, msg4 = engine.validate_and_route_order(cand4)
    assert ok4 is False
    assert "slots currently occupied" in msg4


def test_two_tier_circuit_breaker_and_hard_lock(risk_env):
    engine, broker, db, lock_path = risk_env
    now = datetime.now(timezone.utc)

    # Tier 0: Normal
    assert engine.evaluate_circuit_breaker(95.0) == CircuitBreakerTier.NORMAL

    # Tier 1: Soft Buy Halt (Equity <= $80.00)
    assert engine.evaluate_circuit_breaker(79.50) == CircuitBreakerTier.SOFT_HALT

    # Test that soft halt blocks buy orders
    broker.cash = 79.50
    cand = ScreenedCandidate(
        candidate_id="c_soft",
        timestamp=now,
        ticker="SPY",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=100.0,
        stop_loss=90.0,
        take_profit=120.0,
        risk_r=10.0,
        allocated_usd=25.0,
        rank_score=1.0,
    )
    db.save_candidate(cand)
    ok, _, _, msg = engine.validate_and_route_order(cand)
    assert ok is False
    assert "Soft Freeze active" in msg

    # Tier 2: Hard Liquidation Floor (Equity <= $70.00)
    # Open a position first to test emergency liquidation
    broker.cash = 85.0
    broker.set_price("NVDA", 100.0)
    cand_nvda = ScreenedCandidate(
        candidate_id="c_nvda",
        timestamp=now,
        ticker="NVDA",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=100.0,
        stop_loss=90.0,
        take_profit=120.0,
        risk_r=10.0,
        allocated_usd=30.0,
        rank_score=1.0,
    )
    db.save_candidate(cand_nvda)
    engine.validate_and_route_order(cand_nvda)
    assert len(broker.get_positions()) == 1

    # Simulate catastrophic equity drop to $68.00
    broker.cash = 40.0
    broker.set_price("NVDA", 28.0)  # market value drops
    bal = broker.get_account_balance()
    assert bal.equity <= 70.0

    tier2 = engine.evaluate_circuit_breaker(bal.equity)
    assert tier2 == CircuitBreakerTier.HARD_LIQUIDATION
    assert engine.is_hard_locked() is True
    assert lock_path.exists()

    # Verify emergency liquidation closed all positions
    engine.execute_emergency_liquidation()
    assert len(broker.get_positions()) == 0

    # Operator resume / release lock
    assert engine.release_hard_lock() is True
    assert engine.is_hard_locked() is False


def test_bracket_watchdog_exits(risk_env):
    engine, broker, db, _ = risk_env
    now = datetime.now(timezone.utc)
    broker.set_price("MSFT", 200.0)

    cand = ScreenedCandidate(
        candidate_id="c_msft",
        timestamp=now,
        ticker="MSFT",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=200.0,
        stop_loss=190.0,
        take_profit=220.0,
        risk_r=10.0,
        allocated_usd=30.0,
        rank_score=1.0,
    )
    db.save_candidate(cand)
    engine.validate_and_route_order(cand)
    assert len(broker.get_positions()) == 1

    # 1. Price drops below stop-loss (188.0 < 190.0)
    exits = engine.run_bracket_watchdog({"MSFT": 188.0})
    assert len(exits) == 1
    ticker, reason, price = exits[0]
    assert reason in ("STOP_LOSS", "GAP_STOP")
    assert len(broker.get_positions()) == 0


def test_soft_freeze_engagement_and_release(risk_env):
    engine, broker, db, _ = risk_env
    assert engine.is_soft_freeze_active() is False
    assert db.is_soft_freeze_active() is False

    # Engage soft freeze
    engine.engage_soft_freeze(actor="operator", reason="Pre-CPI volatility")
    assert engine.is_soft_freeze_active() is True
    assert db.is_soft_freeze_active() is True
    assert engine.evaluate_circuit_breaker(100.0) == CircuitBreakerTier.SOFT_HALT

    logs = db.get_audit_logs(limit=5)
    engage_log = next((l for l in logs if l.event_name == "OPERATOR_SOFT_FREEZE_ENGAGED"), None)
    assert engage_log is not None
    assert "Pre-CPI volatility" in engage_log.message

    # Release soft freeze
    engine.release_soft_freeze(actor="operator", reason="CPI announced, risk normal")
    assert engine.is_soft_freeze_active() is False
    assert db.is_soft_freeze_active() is False
    assert engine.evaluate_circuit_breaker(100.0) == CircuitBreakerTier.NORMAL

    logs_after = db.get_audit_logs(limit=5)
    release_log = next((l for l in logs_after if l.event_name == "OPERATOR_SOFT_FREEZE_RELEASED"), None)
    assert release_log is not None
    assert "CPI announced, risk normal" in release_log.message


def test_soft_freeze_blocks_position_sizing_and_order_approval(risk_env):
    engine, broker, db, _ = risk_env
    now = datetime.now(timezone.utc)
    broker.set_price("GOOG", 150.0)

    cand = ScreenedCandidate(
        candidate_id="c_goog",
        timestamp=now,
        ticker="GOOG",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=150.0,
        stop_loss=140.0,
        take_profit=170.0,
        risk_r=10.0,
        allocated_usd=30.0,
        rank_score=1.2,
    )
    db.save_candidate(cand)

    # 1. Engage soft freeze
    engine.engage_soft_freeze(actor="risk_officer", reason="Market-wide halt warning")

    # Position sizing must reject
    ok_size, alloc, qty, size_reason = engine.calculate_position_size(cand, current_cash=100.0)
    assert ok_size is False
    assert alloc == 0.0
    assert qty == 0.0
    assert size_reason.startswith("SOFT_FREEZE_ACTIVE: ")
    assert "Market-wide halt warning" in size_reason

    # Order validation & routing must reject
    ok_route, order, res, route_msg = engine.validate_and_route_order(cand)
    assert ok_route is False
    assert order is None
    assert res is None
    assert route_msg.startswith("SOFT_FREEZE_ACTIVE: ")
    assert "Market-wide halt warning" in route_msg
    assert len(broker.get_positions()) == 0

    # 2. Release soft freeze
    engine.release_soft_freeze(actor="risk_officer", reason="Warning cleared")

    # Sizing and routing now succeed
    ok_size2, alloc2, qty2, _ = engine.calculate_position_size(cand, current_cash=100.0)
    assert ok_size2 is True
    assert alloc2 == 30.0
    assert qty2 > 0

    ok_route2, order2, res2, route_msg2 = engine.validate_and_route_order(cand)
    assert ok_route2 is True
    assert order2 is not None
    assert order2.state == OrderState.FILLED
    assert len(broker.get_positions()) == 1


def test_soft_freeze_persists_across_reinstantiation(risk_env):
    engine, broker, db, lock_path = risk_env
    now = datetime.now(timezone.utc)
    broker.set_price("TSLA", 200.0)

    cand = ScreenedCandidate(
        candidate_id="c_tsla",
        timestamp=now,
        ticker="TSLA",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=200.0,
        stop_loss=180.0,
        take_profit=240.0,
        risk_r=20.0,
        allocated_usd=30.0,
        rank_score=1.1,
    )
    db.save_candidate(cand)

    # Engage soft freeze on initial engine
    engine.engage_soft_freeze(actor="operator", reason="Durable halt across restart")
    assert engine.is_soft_freeze_active() is True

    # 1. New RiskEngine instance with existing db instance
    engine_new_instance = RiskEngine(db=db, broker=broker, lock_file=lock_path)
    assert engine_new_instance.is_soft_freeze_active() is True

    # 2. Completely new Database and RiskEngine instances pointing to same SQLite DB file
    db_reopened = Database(db_path=db.db_path)
    engine_restarted = RiskEngine(db=db_reopened, broker=broker, lock_file=lock_path)
    assert engine_restarted.is_soft_freeze_active() is True

    # Verify that order validation is rejected by the restarted engine
    ok, order, res, msg = engine_restarted.validate_and_route_order(cand)
    assert ok is False
    assert order is None
    assert msg.startswith("SOFT_FREEZE_ACTIVE: ")
    assert "Durable halt across restart" in msg

    # Release on restarted engine
    engine_restarted.release_soft_freeze(actor="operator", reason="Operational restart complete")
    assert engine_restarted.is_soft_freeze_active() is False

    # Verify that a subsequent new RiskEngine reflects the release
    engine_final = RiskEngine(db=db_reopened, broker=broker, lock_file=lock_path)
    assert engine_final.is_soft_freeze_active() is False


def test_unreconciled_account_blocks_entries(risk_env):
    engine, broker, db, _ = risk_env
    db.record_reconciliation_event("evt_bad", "a", "b", "[1]", "UNRESOLVED_DISCREPANCY")
    ok, reason = engine.check_reconciliation_and_pending()
    assert not ok and "unreconciled" in reason


def test_never_reconciled_blocks_entries():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(db_path=path)
    engine = RiskEngine(db=db, broker=SimulatedPaperBroker(initial_cash=100.0), lock_file=Path(path + ".lock"))
    ok, reason = engine.check_reconciliation_and_pending()
    os.remove(path)
    assert not ok and "unreconciled" in reason


def _intent(db, iid, ticker, status, usd=25.0):
    db.save_order_intent(
        intent_id=iid, request_hash=iid, ticker=ticker, side="BUY", target_qty=0.1,
        allocated_usd=usd, idempotency_key=iid, status=status,
    )


def test_unknown_intent_blocks_entries(risk_env):
    engine, _, db, _ = risk_env
    _intent(db, "i1", "MSFT", "UNKNOWN_PENDING_RECONCILIATION")
    ok, reason = engine.check_reconciliation_and_pending()
    assert not ok and "UNKNOWN_PENDING_RECONCILIATION" in reason


def test_pending_intent_reserves_cash(risk_env):
    engine, _, db, _ = risk_env
    _intent(db, "i2", "MSFT", "SUBMITTED", usd=30.0)
    assert engine.reserved_cash() == 30.0


def _routing_candidate(db, sym):
    now = datetime.now(timezone.utc)
    cand = ScreenedCandidate(
        candidate_id=f"c_{sym}", timestamp=now, ticker=sym,
        strategy=StrategyType.TREND_PULLBACK, entry_est=100.0, stop_loss=90.0,
        take_profit=120.0, risk_r=10.0, allocated_usd=30.0, rank_score=1.0,
    )
    db.save_candidate(cand)
    return cand


def test_pending_intents_consume_slots_and_block_duplicates(risk_env):
    engine, broker, db, _ = risk_env
    for s in ("SYM1", "SYM2", "SYM3", "SYM4"):
        broker.set_price(s, 100.0)
    _intent(db, "p1", "SYM1", "SUBMITTED", usd=5.0)
    _intent(db, "p2", "SYM2", "CREATED", usd=5.0)
    ok, _, _, msg = engine.validate_and_route_order(_routing_candidate(db, "SYM1"))
    assert not ok and "Pending order intent for SYM1" in msg
    _intent(db, "p3", "SYM3", "CREATED", usd=5.0)
    ok, _, _, msg = engine.validate_and_route_order(_routing_candidate(db, "SYM4"))
    assert not ok and "reserved by pending orders" in msg
