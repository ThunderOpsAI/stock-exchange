"""
Unit tests for Domain Models and Legal State Transitions (SPEC.md Phase 2, Ticket 10 / P2-02).
Validates TradingRun, OrderIntent, BrokerSubmission, ReconciliationRecord, and ProtectionStatusRecord.
"""

from datetime import datetime, timezone
import pytest

from src.domain.models import (
    BrokerSubmission,
    BrokerSubmissionOutcome,
    IllegalStateTransitionError,
    InstrumentMetadata,
    OrderIntent,
    OrderIntentStatus,
    OrderSide,
    ProtectionMode,
    ProtectionStatusRecord,
    ReconciliationRecord,
    ReconciliationStatus,
    TradingRun,
    TradingRunStatus,
)

pytestmark = pytest.mark.unit


def test_trading_run_valid_lifecycle():
    run = TradingRun(
        run_id="run_123",
        mode="paper",
        config_hash="conf_hash_1",
        trigger="scheduler",
    )
    assert run.status == TradingRunStatus.STARTING
    assert run.ended_at is None

    # Transition to RUNNING
    run.transition_to(TradingRunStatus.RUNNING)
    assert run.status == TradingRunStatus.RUNNING
    assert run.ended_at is None

    # Transition to COMPLETED
    run.transition_to(TradingRunStatus.COMPLETED)
    assert run.status == TradingRunStatus.COMPLETED
    assert run.ended_at is not None


def test_trading_run_failure_and_interruption():
    # STARTING -> FAILED
    run1 = TradingRun(run_id="run_fail", config_hash="h1")
    run1.transition_to(TradingRunStatus.FAILED, error_message="Database lock timeout")
    assert run1.status == TradingRunStatus.FAILED
    assert run1.error_message == "Database lock timeout"
    assert run1.ended_at is not None

    # STARTING -> RUNNING -> INTERRUPTED
    run2 = TradingRun(run_id="run_int", config_hash="h2")
    run2.transition_to(TradingRunStatus.RUNNING)
    run2.transition_to(TradingRunStatus.INTERRUPTED, error_message="SIGINT received")
    assert run2.status == TradingRunStatus.INTERRUPTED
    assert run2.ended_at is not None


def test_trading_run_illegal_transitions():
    run = TradingRun(run_id="run_test", config_hash="h")
    # Illegal direct jump: STARTING -> COMPLETED
    with pytest.raises(IllegalStateTransitionError):
        run.transition_to(TradingRunStatus.COMPLETED)

    # Complete the run
    run.transition_to(TradingRunStatus.RUNNING)
    run.transition_to(TradingRunStatus.COMPLETED)

    # Illegal transition from terminal state
    with pytest.raises(IllegalStateTransitionError):
        run.transition_to(TradingRunStatus.RUNNING)

    with pytest.raises(IllegalStateTransitionError):
        run.transition_to(TradingRunStatus.FAILED)


def test_order_intent_valid_happy_path():
    intent = OrderIntent(
        intent_id="intent_101",
        request_hash="req_101",
        ticker="AAPL",
        side=OrderSide.BUY,
        target_qty=0.5,
        allocated_usd=28.50,
        idempotency_key="key_101",
    )
    assert intent.status == OrderIntentStatus.CREATED

    # CREATED -> SUBMITTED
    intent.transition_to(OrderIntentStatus.SUBMITTED)
    assert intent.status == OrderIntentStatus.SUBMITTED

    # SUBMITTED -> RECONCILED
    intent.transition_to(OrderIntentStatus.RECONCILED)
    assert intent.status == OrderIntentStatus.RECONCILED


def test_order_intent_timeout_and_reconciliation_recovery():
    intent = OrderIntent(
        intent_id="intent_102",
        request_hash="req_102",
        ticker="MSFT",
        side=OrderSide.BUY,
        target_qty=0.1,
        allocated_usd=29.00,
        idempotency_key="key_102",
    )

    intent.transition_to(OrderIntentStatus.SUBMITTED)

    # Broker timeout: transition to UNKNOWN_PENDING_RECONCILIATION
    intent.transition_to(
        OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION,
        reason="HTTP 504 Gateway Timeout from broker",
    )
    assert intent.status == OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION
    assert "Timeout" in (intent.risk_reason or "")

    # Post-restart reconciliation resolves the order
    intent.transition_to(OrderIntentStatus.RECONCILED)
    assert intent.status == OrderIntentStatus.RECONCILED


def test_order_intent_illegal_transitions():
    intent = OrderIntent(
        intent_id="intent_103",
        request_hash="req_103",
        ticker="NVDA",
        side=OrderSide.BUY,
        target_qty=0.2,
        allocated_usd=28.00,
        idempotency_key="key_103",
    )

    # Illegal direct jump: CREATED -> RECONCILED
    with pytest.raises(IllegalStateTransitionError):
        intent.transition_to(OrderIntentStatus.RECONCILED)

    intent.transition_to(OrderIntentStatus.REJECTED, reason="Risk cap exceeded")
    assert intent.status == OrderIntentStatus.REJECTED

    # Illegal transition from terminal state
    with pytest.raises(IllegalStateTransitionError):
        intent.transition_to(OrderIntentStatus.SUBMITTED)

    with pytest.raises(IllegalStateTransitionError):
        intent.transition_to(OrderIntentStatus.RECONCILED)


def test_broker_submission_and_reconciliation_models():
    sub = BrokerSubmission(
        submission_id="sub_001",
        intent_id="intent_001",
        client_order_id="client_001",
        outcome_classification=BrokerSubmissionOutcome.ACCEPTED,
        broker_order_id="broker_ord_1",
    )
    assert sub.outcome_classification == BrokerSubmissionOutcome.ACCEPTED

    rec = ReconciliationRecord(
        event_id="rec_001",
        local_snapshot_hash="local_1",
        broker_snapshot_hash="broker_1",
        resolution_status=ReconciliationStatus.HEALTHY_MATCH,
    )
    assert rec.resolution_status == ReconciliationStatus.HEALTHY_MATCH


def test_protection_status_degradation_logic():
    # Native bracket: healthy
    prot_native = ProtectionStatusRecord(
        protection_id="p1",
        ticker="AAPL",
        protection_mode=ProtectionMode.NATIVE_BRACKET,
        watchdog_healthy=1,
    )
    assert prot_native.is_degraded() is False

    # Watchdog software with healthy watchdog: not degraded
    prot_watchdog = ProtectionStatusRecord(
        protection_id="p2",
        ticker="AAPL",
        protection_mode=ProtectionMode.WATCHDOG_SOFTWARE,
        watchdog_healthy=1,
    )
    assert prot_watchdog.is_degraded() is False

    # Unprotected: degraded
    prot_unprotected = ProtectionStatusRecord(
        protection_id="p3",
        ticker="AAPL",
        protection_mode=ProtectionMode.DEGRADED_UNPROTECTED,
        watchdog_healthy=1,
    )
    assert prot_unprotected.is_degraded() is True

    # Watchdog software with unhealthy watchdog: degraded
    prot_failed_watchdog = ProtectionStatusRecord(
        protection_id="p4",
        ticker="AAPL",
        protection_mode=ProtectionMode.WATCHDOG_SOFTWARE,
        watchdog_healthy=0,
    )
    assert prot_failed_watchdog.is_degraded() is True
