"""
Tests for Broker Reconciliation Service (SPEC.md Phase 2, Ticket 12 / P2-04).
Covers bidirectional order, position, and fill reconciliation, discrepancy detection,
UNKNOWN_PENDING_RECONCILIATION recovery, and cryptographic audit persistence.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest

from src.broker.reconciliation import (
    ReconciliationDiscrepancyType,
    ReconciliationResult,
    ReconciliationService,
)
from src.broker.simulated import SimulatedPaperBroker
from src.domain.models import (
    BrokerSubmissionOutcome,
    Fill,
    Order,
    OrderIntent,
    OrderIntentStatus,
    OrderSide,
    OrderState,
    OrderType,
    Position,
    PositionStatus,
    ReconciliationStatus,
)
from src.storage.db import Database

pytestmark = pytest.mark.integration


@pytest.fixture
def test_env(tmp_path):
    db_file = tmp_path / "test_reconciliation.db"
    db = Database(db_path=str(db_file))
    broker = SimulatedPaperBroker(initial_cash=100.0)
    service = ReconciliationService(db=db, broker=broker)
    return service, broker, db


def test_healthy_match_when_identical(test_env):
    service, broker, db = test_env

    # 1. Populate identical position locally and on broker
    now = datetime.now(timezone.utc)
    pos = Position(
        position_id="pos_aapl_1",
        broker_position_id="broker_pos_aapl_1",
        ticker="AAPL",
        side=OrderSide.BUY,
        qty=0.25,
        entry_price=150.0,
        current_price=150.0,
        stop_loss=140.0,
        take_profit=170.0,
        market_value=37.5,
        unrealized_pnl=0.0,
        status=PositionStatus.OPEN,
        opened_at=now,
    )
    db.create_trading_run(
        run_id="run_test_001",
        mode="paper",
        config_hash="cfg_hash_001",
        trigger="test",
    )
    db.save_position(pos)
    broker.positions["pos_aapl_1"] = pos

    # Reconcile
    result = service.reconcile(run_id="run_test_001")

    assert result.is_clean is True
    assert result.requires_intervention is False
    assert result.status == ReconciliationStatus.HEALTHY_MATCH
    assert len(result.discrepancies) == 0
    assert result.local_snapshot_hash is not None
    assert len(result.local_snapshot_hash) == 64
    assert result.broker_snapshot_hash is not None
    assert len(result.broker_snapshot_hash) == 64


def test_detection_missing_local_order(test_env):
    service, broker, db = test_env

    # Broker has open order, local DB has no record
    broker.get_open_orders = MagicMock(
        return_value=[
            {
                "order_id": "ext_ord_123",
                "client_order_id": "client_ext_123",
                "ticker": "NVDA",
                "side": "BUY",
                "qty": 0.5,
                "filled_qty": 0.0,
                "status": "OPEN",
            }
        ]
    )

    result = service.reconcile()

    assert result.is_clean is False
    assert result.requires_intervention is True
    assert result.status == ReconciliationStatus.UNRESOLVED_DISCREPANCY

    disc_types = [d.discrepancy_type for d in result.discrepancies]
    assert ReconciliationDiscrepancyType.MISSING_LOCAL_ORDER in disc_types

    disc = next(d for d in result.discrepancies if d.discrepancy_type == ReconciliationDiscrepancyType.MISSING_LOCAL_ORDER)
    assert disc.ticker == "NVDA"
    assert disc.entity_id == "ext_ord_123"


def test_detection_missing_broker_order(test_env):
    service, broker, db = test_env

    # Local DB has order marked ROUTED_TO_BROKER, broker has 0 open orders
    order = Order(
        order_id="ord_local_456",
        client_order_id="client_ord_456",
        ticker="MSFT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        allocated_usd=28.0,
        target_qty=0.1,
        state=OrderState.ROUTED_TO_BROKER,
    )
    db.save_order(order)
    broker.get_open_orders = MagicMock(return_value=[])

    result = service.reconcile()

    assert result.is_clean is False
    assert result.requires_intervention is True
    assert result.status == ReconciliationStatus.UNRESOLVED_DISCREPANCY

    disc_types = [d.discrepancy_type for d in result.discrepancies]
    assert ReconciliationDiscrepancyType.MISSING_BROKER_ORDER in disc_types

    disc = next(d for d in result.discrepancies if d.discrepancy_type == ReconciliationDiscrepancyType.MISSING_BROKER_ORDER)
    assert disc.ticker == "MSFT"
    assert disc.entity_id == "ord_local_456"


def test_detection_partial_fill_discrepancy(test_env):
    service, broker, db = test_env

    # Local DB order exists with 0 fills
    order = Order(
        order_id="ord_partial_001",
        client_order_id="client_partial_001",
        ticker="AMZN",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        allocated_usd=30.0,
        target_qty=0.2,
        state=OrderState.ROUTED_TO_BROKER,
    )
    db.save_order(order)

    # Broker reports order as partially filled (0.1 filled out of 0.2)
    broker.get_open_orders = MagicMock(
        return_value=[
            {
                "order_id": "ord_partial_001",
                "client_order_id": "client_partial_001",
                "ticker": "AMZN",
                "side": "BUY",
                "qty": 0.2,
                "filled_qty": 0.1,
                "status": "PARTIALLY_FILLED",
            }
        ]
    )

    result = service.reconcile()

    assert result.is_clean is False
    assert result.requires_intervention is True
    assert result.status == ReconciliationStatus.UNRESOLVED_DISCREPANCY

    disc_types = [d.discrepancy_type for d in result.discrepancies]
    assert ReconciliationDiscrepancyType.PARTIAL_FILL in disc_types

    disc = next(d for d in result.discrepancies if d.discrepancy_type == ReconciliationDiscrepancyType.PARTIAL_FILL)
    assert disc.ticker == "AMZN"
    assert disc.broker_value["filled_qty"] == 0.1
    assert disc.local_value["filled_qty"] == 0.0


def test_detection_position_mismatch(test_env):
    service, broker, db = test_env

    now = datetime.now(timezone.utc)
    # Local DB has 0.5 shares of TSLA
    local_pos = Position(
        position_id="pos_tsla_1",
        broker_position_id="broker_pos_tsla_1",
        ticker="TSLA",
        side=OrderSide.BUY,
        qty=0.5,
        entry_price=200.0,
        current_price=200.0,
        stop_loss=190.0,
        take_profit=230.0,
        market_value=100.0,
        unrealized_pnl=0.0,
        status=PositionStatus.OPEN,
        opened_at=now,
    )
    db.save_position(local_pos)

    # Broker reports 0.3 shares of TSLA
    broker_pos = Position(
        position_id="pos_tsla_broker",
        broker_position_id="broker_pos_tsla_1",
        ticker="TSLA",
        side=OrderSide.BUY,
        qty=0.3,
        entry_price=200.0,
        current_price=200.0,
        stop_loss=190.0,
        take_profit=230.0,
        market_value=60.0,
        unrealized_pnl=0.0,
        status=PositionStatus.OPEN,
        opened_at=now,
    )
    broker.positions["pos_tsla_broker"] = broker_pos

    result = service.reconcile()

    assert result.is_clean is False
    assert result.requires_intervention is True
    assert result.status == ReconciliationStatus.UNRESOLVED_DISCREPANCY

    disc_types = [d.discrepancy_type for d in result.discrepancies]
    assert ReconciliationDiscrepancyType.POSITION_MISMATCH in disc_types

    disc = next(d for d in result.discrepancies if d.discrepancy_type == ReconciliationDiscrepancyType.POSITION_MISMATCH)
    assert disc.ticker == "TSLA"
    assert disc.local_value["qty"] == 0.5
    assert disc.broker_value["qty"] == 0.3


def test_detection_orphaned_broker_position(test_env):
    service, broker, db = test_env

    # Broker holds position that local DB has no record of
    now = datetime.now(timezone.utc)
    orphan_pos = Position(
        position_id="pos_googl_orphan",
        broker_position_id="broker_pos_googl",
        ticker="GOOGL",
        side=OrderSide.BUY,
        qty=0.4,
        entry_price=175.0,
        current_price=175.0,
        stop_loss=165.0,
        take_profit=195.0,
        market_value=70.0,
        unrealized_pnl=0.0,
        status=PositionStatus.OPEN,
        opened_at=now,
    )
    broker.positions["pos_googl_orphan"] = orphan_pos

    result = service.reconcile()

    assert result.is_clean is False
    assert result.requires_intervention is True
    assert result.status == ReconciliationStatus.UNRESOLVED_DISCREPANCY

    disc_types = [d.discrepancy_type for d in result.discrepancies]
    assert ReconciliationDiscrepancyType.ORPHANED_BROKER_POSITION in disc_types

    disc = next(d for d in result.discrepancies if d.discrepancy_type == ReconciliationDiscrepancyType.ORPHANED_BROKER_POSITION)
    assert disc.ticker == "GOOGL"
    assert disc.broker_value["qty"] == 0.4
    assert disc.local_value is None


def test_resolution_unknown_pending_intent_confirmed_filled(test_env):
    service, broker, db = test_env

    # 1. Create OrderIntent in UNKNOWN_PENDING_RECONCILIATION
    intent_id = "intent_unk_001"
    idempotency_key = "idem_unk_001"
    db.save_order_intent(
        intent_id=intent_id,
        request_hash="hash_unk_001",
        ticker="AMD",
        side=OrderSide.BUY.value,
        target_qty=0.25,
        allocated_usd=25.0,
        idempotency_key=idempotency_key,
        status=OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION.value,
    )

    client_order_id = f"ord_amd_{idempotency_key[:8]}"
    db.record_broker_submission(
        submission_id="sub_unk_001",
        intent_id=intent_id,
        client_order_id=client_order_id,
        broker_order_id="broker_amd_999",
        outcome_classification=BrokerSubmissionOutcome.TIMEOUT.value,
    )

    # 2. Broker confirms the order was indeed FILLED and holds position
    now = datetime.now(timezone.utc)
    broker_pos = Position(
        position_id="pos_amd_broker",
        broker_position_id="broker_amd_pos",
        ticker="AMD",
        side=OrderSide.BUY,
        qty=0.25,
        entry_price=100.0,
        current_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        market_value=25.0,
        unrealized_pnl=0.0,
        status=PositionStatus.OPEN,
        opened_at=now,
    )
    broker.positions["pos_amd_broker"] = broker_pos

    broker.get_order = MagicMock(
        return_value={
            "order_id": "broker_amd_999",
            "client_order_id": client_order_id,
            "ticker": "AMD",
            "side": "BUY",
            "qty": 0.25,
            "filled_qty": 0.25,
            "filled_price": 100.0,
            "status": "FILLED",
        }
    )

    # Reconcile
    result = service.reconcile()

    assert result.is_clean is True
    assert result.requires_intervention is False
    assert result.status == ReconciliationStatus.RESOLVED_RECONCILED
    assert intent_id in result.resolved_intents

    # Verify intent in DB transitioned to RECONCILED
    stored_intent = db.get_order_intent(intent_id)
    assert stored_intent["status"] == OrderIntentStatus.RECONCILED.value

    # Verify local Position was created and synchronized
    local_pos = db.get_position_by_ticker("AMD")
    assert local_pos is not None
    assert local_pos.qty == 0.25
    assert local_pos.entry_price == 100.0
    assert local_pos.status == PositionStatus.OPEN

    # Verify Order was synchronized to FILLED
    local_order = db.get_order_by_client_id(client_order_id)
    assert local_order is not None
    assert local_order.state == OrderState.FILLED


def test_resolution_unknown_pending_intent_confirmed_rejected_or_absent(test_env):
    service, broker, db = test_env

    # 1. Create OrderIntent in UNKNOWN_PENDING_RECONCILIATION
    intent_id = "intent_unk_002"
    idempotency_key = "idem_unk_002"
    db.save_order_intent(
        intent_id=intent_id,
        request_hash="hash_unk_002",
        ticker="INTC",
        side=OrderSide.BUY.value,
        target_qty=1.0,
        allocated_usd=20.0,
        idempotency_key=idempotency_key,
        status=OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION.value,
    )

    client_order_id = f"ord_intc_{idempotency_key[:8]}"
    db.record_broker_submission(
        submission_id="sub_unk_002",
        intent_id=intent_id,
        client_order_id=client_order_id,
        broker_order_id=None,
        outcome_classification=BrokerSubmissionOutcome.TIMEOUT.value,
    )

    # Broker has 0 orders and 0 positions (order confirmed absent / never reached broker)
    broker.get_open_orders = MagicMock(return_value=[])

    # Reconcile
    result = service.reconcile()

    assert result.is_clean is True
    assert result.requires_intervention is False
    assert result.status == ReconciliationStatus.RESOLVED_RECONCILED
    assert intent_id in result.resolved_intents

    # Verify intent in DB transitioned to REJECTED
    stored_intent = db.get_order_intent(intent_id)
    assert stored_intent["status"] == OrderIntentStatus.REJECTED.value


def test_db_recording_of_reconciliation_events(test_env):
    service, broker, db = test_env

    # Create trading run
    db.create_trading_run(
        run_id="run_audit_001",
        mode="paper",
        config_hash="cfg_audit_001",
        trigger="test",
    )

    # Create orphaned position on broker to produce discrepancies
    now = datetime.now(timezone.utc)
    orphan_pos = Position(
        position_id="pos_meta_orphan",
        broker_position_id="broker_pos_meta",
        ticker="META",
        side=OrderSide.BUY,
        qty=0.1,
        entry_price=500.0,
        current_price=500.0,
        stop_loss=480.0,
        take_profit=540.0,
        market_value=50.0,
        unrealized_pnl=0.0,
        status=PositionStatus.OPEN,
        opened_at=now,
    )
    broker.positions["pos_meta_orphan"] = orphan_pos

    result = service.reconcile(run_id="run_audit_001")

    # Verify event stored in DB
    rec_event = db.get_reconciliation_event(result.event_id)
    assert rec_event is not None
    assert rec_event["run_id"] == "run_audit_001"
    assert rec_event["resolution_status"] == ReconciliationStatus.UNRESOLVED_DISCREPANCY.value
    assert rec_event["local_snapshot_hash"] == result.local_snapshot_hash
    assert rec_event["broker_snapshot_hash"] == result.broker_snapshot_hash

    # Verify JSON mismatches payload
    mismatches = json.loads(rec_event["mismatches_json"])
    assert len(mismatches) == 1
    assert mismatches[0]["discrepancy_type"] == ReconciliationDiscrepancyType.ORPHANED_BROKER_POSITION.value
    assert mismatches[0]["ticker"] == "META"

    # Verify audit log was recorded
    audit_logs = db.get_audit_logs(component="ReconciliationService")
    assert len(audit_logs) >= 1
    assert audit_logs[0].event_name == "RECONCILIATION_RUN_COMPLETED"
