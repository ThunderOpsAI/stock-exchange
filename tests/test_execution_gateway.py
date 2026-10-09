"""
Unit tests for ExecutionGateway around paper brokers (SPEC.md Phase 2, Ticket 11 / P2-03).
Validates ADR 0001 paper-only enforcement, idempotency deduplication,
and ADR 0002 classified retry/timeout outcomes (UNKNOWN_PENDING_RECONCILIATION).
"""

from unittest.mock import MagicMock
import pytest

from src.broker.gateway import ExecutionGateway
from src.broker.simulated import SimulatedPaperBroker
from src.domain.models import (
    BrokerSubmissionOutcome,
    OrderIntent,
    OrderIntentStatus,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderState,
)
from src.storage.db import Database

pytestmark = pytest.mark.integration


@pytest.fixture
def gateway_env(tmp_path):
    db_path = str(tmp_path / "test_gateway.db")
    db = Database(db_path=db_path)
    broker = SimulatedPaperBroker(initial_cash=100.0)
    gateway = ExecutionGateway(broker=broker, db=db, mode="paper")
    return gateway, broker, db


def test_successful_order_submission(gateway_env):
    gateway, broker, db = gateway_env

    intent = OrderIntent(
        intent_id="intent_001",
        request_hash="hash_001",
        ticker="AAPL",
        side=OrderSide.BUY,
        target_qty=0.25,
        allocated_usd=28.50,
        idempotency_key="idem_uuid_001",
        stop_loss=110.0,
        take_profit=125.0,
    )

    result = gateway.execute_intent(intent)

    assert result.success is True
    assert result.outcome == BrokerSubmissionOutcome.ACCEPTED
    assert result.intent_status == OrderIntentStatus.SUBMITTED
    assert result.broker_order_id is not None
    assert result.is_idempotent_duplicate is False

    # Check persistence
    stored_intent = db.get_order_intent("intent_001")
    assert stored_intent["status"] == "SUBMITTED"

    subs = db.get_submissions_for_intent("intent_001")
    assert len(subs) == 1
    assert subs[0]["outcome_classification"] == "ACCEPTED"


def test_idempotent_duplicate_submission(gateway_env):
    gateway, broker, db = gateway_env

    intent = OrderIntent(
        intent_id="intent_002",
        request_hash="hash_002",
        ticker="MSFT",
        side=OrderSide.BUY,
        target_qty=0.1,
        allocated_usd=29.00,
        idempotency_key="idem_uuid_002",
    )

    # First execution
    res1 = gateway.execute_intent(intent)
    assert res1.success is True
    assert res1.is_idempotent_duplicate is False
    broker_order_id = res1.broker_order_id

    # Second execution with same idempotency key (different instance or retry)
    intent_retry = OrderIntent(
        intent_id="intent_002_retry",
        request_hash="hash_002_retry",
        ticker="MSFT",
        side=OrderSide.BUY,
        target_qty=0.1,
        allocated_usd=29.00,
        idempotency_key="idem_uuid_002",  # Matching key
    )

    res2 = gateway.execute_intent(intent_retry)
    assert res2.success is True
    assert res2.is_idempotent_duplicate is True
    assert res2.broker_order_id == broker_order_id

    # Ensure only 1 submission was made to the broker
    subs = db.get_submissions_for_intent("intent_002")
    assert len(subs) == 1


def test_timeout_outcome_classification(gateway_env):
    gateway, broker, db = gateway_env

    # Mock broker to raise TimeoutError
    broker.submit_order = MagicMock(side_effect=TimeoutError("Connection timed out waiting for ACK"))

    intent = OrderIntent(
        intent_id="intent_003",
        request_hash="hash_003",
        ticker="NVDA",
        side=OrderSide.BUY,
        target_qty=0.2,
        allocated_usd=28.00,
        idempotency_key="idem_uuid_003",
    )

    result = gateway.execute_intent(intent)

    # ADR 0002: Must NOT blind retry; must route to UNKNOWN_PENDING_RECONCILIATION
    assert result.success is False
    assert result.outcome == BrokerSubmissionOutcome.TIMEOUT
    assert result.intent_status == OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION
    assert result.requires_reconciliation is True
    assert "timed out" in result.error_message

    # Verify database state
    stored_intent = db.get_order_intent("intent_003")
    assert stored_intent["status"] == "UNKNOWN_PENDING_RECONCILIATION"

    subs = db.get_submissions_for_intent("intent_003")
    assert len(subs) == 1
    assert subs[0]["outcome_classification"] == "TIMEOUT"


def test_duplicate_submission_blocked_when_pending_reconciliation(gateway_env):
    gateway, broker, db = gateway_env

    broker.submit_order = MagicMock(side_effect=TimeoutError("Gateway timeout"))

    intent = OrderIntent(
        intent_id="intent_004",
        request_hash="hash_004",
        ticker="SPY",
        side=OrderSide.BUY,
        target_qty=0.05,
        allocated_usd=25.00,
        idempotency_key="idem_uuid_004",
    )

    res1 = gateway.execute_intent(intent)
    assert res1.intent_status == OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION

    # Re-submission attempt while in UNKNOWN_PENDING_RECONCILIATION
    res2 = gateway.execute_intent(intent)
    assert res2.success is False
    assert res2.outcome == BrokerSubmissionOutcome.UNKNOWN_PENDING_RECONCILIATION
    assert res2.requires_reconciliation is True
    assert res2.is_idempotent_duplicate is True


def test_broker_rejection_classification(gateway_env):
    gateway, broker, db = gateway_env

    # Simulate broker rejection
    rejected_res = OrderResult(
        order_id="rej_ord_001",
        client_order_id="client_rej_001",
        ticker="XYZ",
        status=OrderState.REJECTED_BY_BROKER,
        error_message="Insufficient liquidity or market closed",
    )
    broker.submit_order = MagicMock(return_value=rejected_res)

    intent = OrderIntent(
        intent_id="intent_005",
        request_hash="hash_005",
        ticker="XYZ",
        side=OrderSide.BUY,
        target_qty=1.0,
        allocated_usd=20.00,
        idempotency_key="idem_uuid_005",
    )

    result = gateway.execute_intent(intent)
    assert result.success is False
    assert result.outcome == BrokerSubmissionOutcome.REJECTED
    assert result.intent_status == OrderIntentStatus.REJECTED

    stored = db.get_order_intent("intent_005")
    assert stored["status"] == "REJECTED"


def test_live_trading_mode_blocked(tmp_path):
    db_path = str(tmp_path / "test_live_guard.db")
    db = Database(db_path=db_path)
    broker = SimulatedPaperBroker(initial_cash=100.0)

    # ADR 0001: Attempting to instantiate gateway in live mode must raise RuntimeError
    with pytest.raises(RuntimeError, match="strictly forbidden by ADR 0001"):
        ExecutionGateway(broker=broker, db=db, mode="live")

    with pytest.raises(RuntimeError, match="strictly forbidden by ADR 0001"):
        ExecutionGateway(broker=broker, db=db, mode="real_capital")
