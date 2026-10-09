"""
Unit tests for TelegramTransportAdapter, sender authorization, callback acknowledgement, and replay protection.
Verifies:
- Unauthorized users cannot act
- Callback replay is harmless
- Transport errors are audited into SQLite audit_logs
- Tests use mocked HTTP only
"""

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.broker.simulated import SimulatedPaperBroker
from src.domain.models import (
    AuditSeverity,
    CandidateStatus,
    CommitteeVerdict,
    HITLStatus,
    LLMDeliberation,
    ScreenedCandidate,
    StrategyType,
    VerdictOutcome,
)
from src.observability.telegram_bot import (
    TelegramBotHandler,
    TelegramTransportAdapter,
    TelegramTransportError,
)
from src.risk.engine import RiskEngine
from src.storage.db import Database


@pytest.fixture
def test_env():
    fd_db, path_db = tempfile.mkstemp(suffix=".db")
    os.close(fd_db)
    db = Database(db_path=path_db)

    fd_lock, path_lock = tempfile.mkstemp(suffix=".lock")
    os.close(fd_lock)
    os.remove(path_lock)
    lock_path = Path(path_lock)

    broker = SimulatedPaperBroker(initial_cash=100.0)
    risk_engine = RiskEngine(db=db, broker=broker, lock_file=lock_path)
    db.record_reconciliation_event("evt_clean", "h_loc", "h_brk", "[]", "HEALTHY_MATCH")

    session = requests.Session()
    transport = TelegramTransportAdapter(
        bot_token="test_token_123",
        session=session,
        db=db,
        base_url="https://mock.telegram.api",
    )

    handler = TelegramBotHandler(
        db=db,
        broker=broker,
        risk_engine=risk_engine,
        bot_token="test_token_123",
        chat_id="1001",
        allowed_user_ids={"12345", "67890"},
        transport=transport,
    )

    yield handler, transport, broker, db, risk_engine, session

    if os.path.exists(path_db):
        os.remove(path_db)
    if lock_path.exists():
        lock_path.unlink()


def test_sender_authorization_authorized_user(test_env):
    handler, transport, broker, db, risk_engine, _ = test_env
    # User 12345 is in allowed_user_ids
    res = handler.dispatch_command("/status", user_id="12345")
    assert "PORTFOLIO STATUS" in res


def test_sender_authorization_unauthorized_user_blocked_and_audited(test_env):
    handler, transport, broker, db, risk_engine, _ = test_env
    # User 99999 is NOT in allowed_user_ids
    res = handler.dispatch_command("/status", user_id="99999")
    assert "Unauthorized" in res

    # Verify audit log was recorded
    audit_logs = db.get_audit_logs()
    unauth_logs = [l for l in audit_logs if l.event_name == "UNAUTHORIZED_TELEGRAM_ACCESS"]
    assert len(unauth_logs) >= 1
    assert "99999" in unauth_logs[-1].message


def test_sender_authorization_unauthorized_callback_blocked_and_audited(test_env):
    handler, transport, broker, db, risk_engine, _ = test_env

    with patch.object(transport, "answer_callback_query") as mock_ack:
        ok, msg = handler.dispatch_callback("cb_q_1", "hitl_approve:cand_test", user_id="99999")
        assert ok is False
        assert "Unauthorized" in msg
        mock_ack.assert_called_once()
        assert "Unauthorized" in mock_ack.call_args[1]["text"]

    audit_logs = db.get_audit_logs()
    unauth_logs = [l for l in audit_logs if l.event_name == "UNAUTHORIZED_TELEGRAM_ACCESS"]
    assert len(unauth_logs) >= 1


def test_callback_replay_protection_is_harmless(test_env):
    handler, transport, broker, db, risk_engine, _ = test_env
    now = datetime.now(timezone.utc)
    broker.set_price("AAPL", 150.0)

    # Seed candidate and verdict
    cand = ScreenedCandidate(
        candidate_id="cand_replay_1",
        timestamp=now,
        ticker="AAPL",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=150.0,
        stop_loss=145.0,
        take_profit=160.0,
        risk_r=5.0,
        allocated_usd=28.50,
        rank_score=1.5,
        status=CandidateStatus.HITL_ESCALATED,
    )
    db.save_candidate(cand)

    verdict = CommitteeVerdict(
        verdict_id="verd_replay_1",
        candidate_id="cand_replay_1",
        ticker="AAPL",
        timestamp=now,
        composite_score=60.0,
        verdict_outcome=VerdictOutcome.HITL_ESCALATED,
        risk_officer_dissent=True,
        hitl_status=HITLStatus.PENDING_TELEGRAM_RESPONSE,
    )
    db.save_committee_verdict(verdict)

    with patch.object(transport, "answer_callback_query") as mock_ack:
        # First call: approved and order routed
        ok1, msg1 = handler.dispatch_callback("cb_id_unique_1", "hitl_approve:cand_replay_1", user_id="12345")
        assert ok1 is True
        assert "APPROVED" in msg1
        assert len(broker.get_positions()) == 1

        # Second call with same callback_query_id: harmless replay
        ok2, msg2 = handler.dispatch_callback("cb_id_unique_1", "hitl_approve:cand_replay_1", user_id="12345")
        assert ok2 is True
        assert "replay harmless" in msg2
        # Position count must not change
        assert len(broker.get_positions()) == 1

        # Third call with new callback_query_id but already resolved candidate: harmless
        ok3, msg3 = handler.dispatch_callback("cb_id_unique_2", "hitl_approve:cand_replay_1", user_id="12345")
        assert ok3 is True
        assert "already resolved" in msg3
        assert len(broker.get_positions()) == 1


def test_transport_send_message_mocked_http(test_env):
    handler, transport, broker, db, risk_engine, session = test_env

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"ok": True, "result": {"message_id": 42}}

    with patch.object(session, "request", return_value=mock_resp) as mock_req:
        res = transport.send_message(chat_id="1001", text="Hello Desk")
        assert res["ok"] is True
        assert res["result"]["message_id"] == 42
        mock_req.assert_called_once()
        assert mock_req.call_args[1]["json"]["chat_id"] == "1001"
        assert mock_req.call_args[1]["json"]["text"] == "Hello Desk"


def test_transport_error_auditing(test_env):
    handler, transport, broker, db, risk_engine, session = test_env

    # Simulate network timeout/error
    with patch.object(session, "request", side_effect=requests.ConnectTimeout("Connection timed out")):
        with pytest.raises(TelegramTransportError) as exc_info:
            transport.send_message(chat_id="1001", text="Will fail")
        assert "timed out" in str(exc_info.value)

    # Verify error is audited in SQLite
    audit_logs = db.get_audit_logs()
    err_logs = [l for l in audit_logs if l.event_name == "TELEGRAM_TRANSPORT_ERROR"]
    assert len(err_logs) >= 1
    assert "timed out" in err_logs[-1].message


def test_transport_api_error_response_auditing(test_env):
    handler, transport, broker, db, risk_engine, session = test_env

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"ok": False, "description": "Bad Request: chat not found"}

    with patch.object(session, "request", return_value=mock_resp):
        with pytest.raises(TelegramTransportError) as exc_info:
            transport.send_message(chat_id="9999", text="Hello")
        assert "Bad Request: chat not found" in str(exc_info.value)

    audit_logs = db.get_audit_logs()
    err_logs = [l for l in audit_logs if l.event_name == "TELEGRAM_TRANSPORT_ERROR"]
    assert len(err_logs) >= 1
    assert "chat not found" in err_logs[-1].message


def test_process_update_message_and_webhook(test_env):
    handler, transport, broker, db, risk_engine, _ = test_env

    update = {
        "update_id": 101,
        "message": {
            "message_id": 500,
            "from": {"id": 12345, "first_name": "Trader"},
            "chat": {"id": 1001},
            "text": "/status",
        },
    }

    with patch.object(transport, "send_message") as mock_send:
        res = handler.process_update(update)
        assert res["type"] == "message"
        assert "PORTFOLIO STATUS" in res["response"]
        mock_send.assert_called_once_with(chat_id=1001, text=res["response"])


def test_polling_loop_mocked(test_env):
    handler, transport, broker, db, risk_engine, _ = test_env

    updates = [
        {
            "update_id": 201,
            "message": {
                "from": {"id": 12345},
                "chat": {"id": 1001},
                "text": "/status",
            },
        },
        {
            "update_id": 202,
            "message": {
                "from": {"id": 99999},  # unauthorized
                "chat": {"id": 1001},
                "text": "/status",
            },
        },
    ]

    with patch.object(transport, "get_updates", return_value=updates):
        with patch.object(transport, "send_message"):
            results = handler.poll_once(timeout=0)
            assert len(results) == 2
            assert "PORTFOLIO STATUS" in results[0]["response"]
            assert "Unauthorized" in results[1]["response"]
            assert handler.update_offset == 203
