"""
Tests for Broker Protection Service (P3-01 / P3-02).
"""

import os
import tempfile
from datetime import datetime, timezone
import pytest

from src.broker.base import AbstractBrokerAdapter
from src.broker.etoro import EtoroBrokerAdapter
from src.broker.alpaca import AlpacaPaperBroker
from src.broker.simulated import SimulatedPaperBroker
from src.broker.protection import BrokerProtectionService
from src.domain.models import (
    OrderRequest,
    OrderSide,
    OrderType,
    OrderState,
    OrderResult,
    Position,
    PositionStatus,
    ProtectionMode,
)
from src.storage.db import Database


@pytest.fixture
def protection_env():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(db_path=path)
    broker = SimulatedPaperBroker(initial_cash=100.0)
    service = BrokerProtectionService(db=db, broker=broker)

    yield service, broker, db

    if os.path.exists(path):
        os.remove(path)


def test_broker_capabilities_descriptor():
    sim = SimulatedPaperBroker()
    assert sim.supports_native_bracket is True

    alpaca = AlpacaPaperBroker(api_key="mock", secret_key="mock")
    assert alpaca.supports_native_bracket is True

    etoro = EtoroBrokerAdapter(api_key="mock", user_key="mock")
    assert etoro.supports_native_bracket is False


def test_native_bracket_protection_recorded(protection_env):
    service, broker, db = protection_env

    # Submit a bracket order with SL and TP
    req = OrderRequest(
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.BRACKET,
        dollar_amount=25.0,
        stop_loss=95.0,
        take_profit=115.0,
        client_order_id="client_aapl_1",
    )
    res = broker.submit_order(req)
    assert res.status == OrderState.FILLED
    assert res.raw_response is not None
    assert "stop_loss_order_id" in res.raw_response

    pos = broker.get_position("AAPL")
    assert pos is not None

    record = service.record_entry_protection(res, pos)
    assert record.protection_mode == ProtectionMode.NATIVE_BRACKET
    assert record.stop_loss_order_id == res.raw_response["stop_loss_order_id"]
    assert record.take_profit_order_id == res.raw_response["take_profit_order_id"]
    assert record.watchdog_healthy == 1

    # Verify persisted in database
    db_record = db.get_protection_status_for_position(pos.position_id)
    assert db_record is not None
    assert db_record["protection_mode"] == ProtectionMode.NATIVE_BRACKET.value
    assert db_record["stop_loss_order_id"] == record.stop_loss_order_id


def test_software_watchdog_fallback_for_unsupported_broker(protection_env):
    service, broker, db = protection_env

    class MockUnsupportedBroker(SimulatedPaperBroker):
        @property
        def supports_native_bracket(self) -> bool:
            return False

    unsupported_broker = MockUnsupportedBroker()
    service_unsupported = BrokerProtectionService(db=db, broker=unsupported_broker)

    fake_result = OrderResult(
        order_id="ord_mock",
        client_order_id="client_mock",
        ticker="MSFT",
        status=OrderState.FILLED,
    )
    now = datetime.now(timezone.utc)
    pos = Position(
        position_id="pos_msft",
        ticker="MSFT",
        side=OrderSide.BUY,
        qty=1.0,
        entry_price=200.0,
        current_price=200.0,
        stop_loss=190.0,
        take_profit=220.0,
        market_value=200.0,
        unrealized_pnl=0.0,
        status=PositionStatus.OPEN,
        opened_at=now,
    )

    record = service_unsupported.record_entry_protection(
        fake_result, pos, allow_watchdog_fallback=True
    )
    assert record.protection_mode == ProtectionMode.WATCHDOG_SOFTWARE
    assert record.watchdog_healthy == 1


def test_degraded_unprotected_when_no_native_and_no_watchdog(protection_env):
    service, broker, db = protection_env

    class MockUnsupportedBroker(SimulatedPaperBroker):
        @property
        def supports_native_bracket(self) -> bool:
            return False

    service_unsupported = BrokerProtectionService(db=db, broker=MockUnsupportedBroker())

    fake_result = OrderResult(
        order_id="ord_mock",
        client_order_id="client_mock",
        ticker="GOOGL",
        status=OrderState.FILLED,
    )
    now = datetime.now(timezone.utc)
    pos = Position(
        position_id="pos_googl",
        ticker="GOOGL",
        side=OrderSide.BUY,
        qty=0.5,
        entry_price=150.0,
        current_price=150.0,
        stop_loss=140.0,
        take_profit=170.0,
        market_value=75.0,
        unrealized_pnl=0.0,
        status=PositionStatus.OPEN,
        opened_at=now,
    )

    record = service_unsupported.record_entry_protection(
        fake_result, pos, allow_watchdog_fallback=False
    )
    assert record.protection_mode == ProtectionMode.DEGRADED_UNPROTECTED
    assert record.watchdog_healthy == 0
    assert "disabled" in (record.degradation_reason or "")


def test_verify_native_legs_reconciliation_and_degradation(protection_env):
    service, broker, db = protection_env

    req = OrderRequest(
        ticker="NVDA",
        side=OrderSide.BUY,
        order_type=OrderType.BRACKET,
        dollar_amount=20.0,
        stop_loss=100.0,
        take_profit=130.0,
        client_order_id="client_nvda",
    )
    res = broker.submit_order(req)
    pos = broker.get_position("NVDA")
    assert pos is not None

    service.record_entry_protection(res, pos)

    # 1. Verification passes when open orders match
    ok, err = service.verify_native_legs(pos)
    assert ok is True
    assert err is None

    # 2. Simulate broker leg order disappearing (e.g. cancelled externally)
    sl_id = res.raw_response["stop_loss_order_id"]
    broker.open_orders.pop(sl_id, None)

    # 3. Verification must fail and mark position as DEGRADED_UNPROTECTED
    ok, err = service.verify_native_legs(pos)
    assert ok is False
    assert "missing" in (err or "").lower()

    db_status = db.get_protection_status_for_position(pos.position_id)
    assert db_status["protection_mode"] == ProtectionMode.DEGRADED_UNPROTECTED.value
    assert db_status["watchdog_healthy"] == 0
