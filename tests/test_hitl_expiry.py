"""
Unit tests for HITL timeout expiry, atomic single terminal state transitions,
and deterministic risk engine validation on human approval.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Tuple

import pytest

from src.broker.simulated import SimulatedPaperBroker
from src.domain.models import (
    CandidateStatus,
    CommitteeVerdict,
    HITLStatus,
    ScreenedCandidate,
    StrategyType,
    VerdictOutcome,
)
from src.llm.hitl import HITLDecisionManager
from src.risk.engine import RiskEngine
from src.storage.db import Database


@pytest.fixture
def hitl_env():
    fd_db, path_db = tempfile.mkstemp(suffix=".db")
    os.close(fd_db)
    db = Database(db_path=path_db)

    fd_lock, path_lock = tempfile.mkstemp(suffix=".lock")
    os.close(fd_lock)
    os.remove(path_lock)
    lock_path = Path(path_lock)

    broker = SimulatedPaperBroker(initial_cash=100.0)
    broker.set_price("NVDA", 120.0)
    risk_engine = RiskEngine(db=db, broker=broker, lock_file=lock_path)
    db.record_reconciliation_event("evt_clean", "h_local", "h_broker", "[]", "HEALTHY_MATCH")

    manager = HITLDecisionManager(db=db, risk_engine=risk_engine, timeout_seconds=900.0)

    yield manager, db, risk_engine, broker, lock_path

    if os.path.exists(path_db):
        os.remove(path_db)
    if lock_path.exists():
        lock_path.unlink()


def create_candidate_and_verdict(
    db: Database,
    cand_id: str,
    ticker: str = "NVDA",
    created_at: datetime = None,
) -> Tuple[ScreenedCandidate, CommitteeVerdict]:
    now = created_at or datetime.now(timezone.utc)
    cand = ScreenedCandidate(
        candidate_id=cand_id,
        timestamp=now,
        ticker=ticker,
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=120.0,
        stop_loss=115.0,
        take_profit=130.0,
        risk_r=5.0,
        allocated_usd=28.50,
        rank_score=1.8,
        status=CandidateStatus.HITL_ESCALATED,
    )
    db.save_candidate(cand)

    verd = CommitteeVerdict(
        verdict_id=f"v_{cand_id}",
        candidate_id=cand_id,
        ticker=ticker,
        timestamp=now,
        composite_score=68.0,
        verdict_outcome=VerdictOutcome.HITL_ESCALATED,
        risk_officer_dissent=True,
        hitl_status=HITLStatus.PENDING_TELEGRAM_RESPONSE,
    )
    db.save_committee_verdict(verd)
    return cand, verd


def test_approve_within_window_succeeds_and_routes_to_risk(hitl_env):
    manager, db, risk_engine, broker, _ = hitl_env
    t0 = datetime.now(timezone.utc)
    cand, verd = create_candidate_and_verdict(db, "cand_fast_1", "NVDA", created_at=t0)

    # Approve at t0 + 2 minutes
    t_approve = t0 + timedelta(minutes=2)
    ok, msg, order = manager.approve("cand_fast_1", actor="operator_alice", as_of=t_approve)

    assert ok is True
    assert "Trade approved and routed" in msg
    assert order is not None
    assert order.ticker == "NVDA"

    # Check persistence
    stored_cand = db.get_candidate("cand_fast_1")
    assert stored_cand.status == CandidateStatus.APPROVED

    stored_verd = db.get_committee_verdict("cand_fast_1")
    assert stored_verd.hitl_status == HITLStatus.HUMAN_APPROVED

    # Check broker received position
    assert len(broker.get_positions()) == 1


def test_approve_after_expiry_fails(hitl_env):
    manager, db, risk_engine, broker, _ = hitl_env
    t0 = datetime.now(timezone.utc)
    cand, verd = create_candidate_and_verdict(db, "cand_late_1", "NVDA", created_at=t0)

    # Attempt approve at t0 + 16 minutes (> 15 minutes limit)
    t_late = t0 + timedelta(minutes=16)
    ok, msg, order = manager.approve("cand_late_1", actor="operator_bob", as_of=t_late)

    assert ok is False
    assert "Approve failed" in msg
    assert "limit" in msg or "expired" in msg
    assert order is None

    # Check that candidate and verdict transitioned to TIMED_OUT / EXPIRED
    stored_cand = db.get_candidate("cand_late_1")
    assert stored_cand.status == CandidateStatus.EXPIRED

    stored_verd = db.get_committee_verdict("cand_late_1")
    assert stored_verd.hitl_status == HITLStatus.TIMED_OUT

    # No order on broker
    assert len(broker.get_positions()) == 0

    # Verify audit log was recorded
    audit_logs = db.get_audit_logs()
    exp_logs = [l for l in audit_logs if l.event_name == "HITL_WINDOW_EXPIRED"]
    assert len(exp_logs) >= 1
    assert "cand_late_1" in exp_logs[-1].message


def test_reject_after_expiry_fails(hitl_env):
    manager, db, risk_engine, broker, _ = hitl_env
    t0 = datetime.now(timezone.utc)
    cand, verd = create_candidate_and_verdict(db, "cand_late_2", "NVDA", created_at=t0)

    # Attempt reject at t0 + 20 minutes
    t_late = t0 + timedelta(minutes=20)
    ok, msg = manager.reject("cand_late_2", actor="operator_bob", as_of=t_late)

    assert ok is False
    assert "Reject failed" in msg
    assert "expired" in msg or "limit" in msg

    # State is TIMED_OUT / EXPIRED
    stored_verd = db.get_committee_verdict("cand_late_2")
    assert stored_verd.hitl_status == HITLStatus.TIMED_OUT


def test_only_one_terminal_outcome_possible(hitl_env):
    manager, db, risk_engine, broker, _ = hitl_env
    t0 = datetime.now(timezone.utc)
    cand, verd = create_candidate_and_verdict(db, "cand_term_1", "NVDA", created_at=t0)

    # 1. Operator rejects candidate
    ok1, msg1 = manager.reject("cand_term_1", actor="operator_alice", reason="Too much volatility", as_of=t0 + timedelta(minutes=1))
    assert ok1 is True

    stored_verd = db.get_committee_verdict("cand_term_1")
    assert stored_verd.hitl_status == HITLStatus.HUMAN_REJECTED

    # 2. Subsequent attempt to approve MUST fail
    ok2, msg2, order2 = manager.approve("cand_term_1", actor="operator_bob", as_of=t0 + timedelta(minutes=2))
    assert ok2 is False
    assert "Approve failed" in msg2
    assert "non-pending" in msg2 or "terminal" in msg2
    assert order2 is None

    # Status remains HUMAN_REJECTED
    stored_verd_after = db.get_committee_verdict("cand_term_1")
    assert stored_verd_after.hitl_status == HITLStatus.HUMAN_REJECTED


def test_risk_validation_still_runs_on_human_approval(hitl_env):
    manager, db, risk_engine, broker, _ = hitl_env
    t0 = datetime.now(timezone.utc)
    cand, verd = create_candidate_and_verdict(db, "cand_risk_fail_1", "NVDA", created_at=t0)

    # Engage persistent soft freeze on risk engine
    risk_engine.engage_soft_freeze("admin", "Macro risk warning")

    # Operator attempts to approve candidate
    ok, msg, order = manager.approve("cand_risk_fail_1", actor="operator_alice", as_of=t0 + timedelta(minutes=3))

    # Must fail execution because Risk Engine blocked it
    assert ok is False
    assert "Risk Engine blocked execution" in msg
    assert "SOFT_FREEZE" in msg or "Soft freeze" in msg
    assert order is None

    # Broker has NO position
    assert len(broker.get_positions()) == 0

    # Audit log recorded
    audit_logs = db.get_audit_logs()
    risk_blocked_logs = [l for l in audit_logs if l.event_name == "HITL_APPROVAL_RISK_BLOCKED"]
    assert len(risk_blocked_logs) >= 1


def test_sweep_expired_batches(hitl_env):
    manager, db, risk_engine, broker, _ = hitl_env
    t0 = datetime.now(timezone.utc)

    # cand_old: 20 minutes ago
    create_candidate_and_verdict(db, "cand_old", "AAPL", created_at=t0 - timedelta(minutes=20))
    # cand_recent: 5 minutes ago
    create_candidate_and_verdict(db, "cand_recent", "MSFT", created_at=t0 - timedelta(minutes=5))

    expired = manager.sweep_expired(as_of=t0)
    assert "cand_old" in expired
    assert "cand_recent" not in expired

    # Check status of both
    v_old = db.get_committee_verdict("cand_old")
    assert v_old.hitl_status == HITLStatus.TIMED_OUT

    v_recent = db.get_committee_verdict("cand_recent")
    assert v_recent.hitl_status == HITLStatus.PENDING_TELEGRAM_RESPONSE
