"""
Unit tests for structured JSON logging, secret redaction, alert routing, and operational runbook.
Verifies:
- Critical conditions create an audit event and alert payload
- Secrets and tokens are redacted
- Runbook covers operational incident recovery
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.domain.models import AuditSeverity
from src.observability.logging import (
    AlertPayload,
    AlertRouter,
    InMemoryAlertSink,
    SecretRedactor,
    StructuredJsonFormatter,
    TelegramAlertSink,
)
from src.storage.db import Database


@pytest.fixture
def obs_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(db_path=path)
    yield db
    if os.path.exists(path):
        os.remove(path)


def test_secret_redaction_strings():
    # 1. Telegram bot token
    raw = "Connecting to bot with token 123456789:ABCdefGHIjklMNOpqrsTUVwxyz-12345 to listen"
    clean = SecretRedactor.redact_string(raw)
    assert "123456789:ABCdefGHIjklMNOpqrsTUVwxyz-12345" not in clean
    assert "[REDACTED_TELEGRAM_TOKEN]" in clean

    # 2. Bearer token
    raw_bearer = "Authorization: Bearer mysecrettoken123456789"
    clean_bearer = SecretRedactor.redact_string(raw_bearer)
    assert "mysecrettoken123456789" not in clean_bearer
    assert "Bearer [REDACTED_TOKEN]" in clean_bearer

    # 3. URL credentials
    raw_url = "postgresql://trader_user:super_secret_pw@db.internal:5432/trading"
    clean_url = SecretRedactor.redact_string(raw_url)
    assert "super_secret_pw" not in clean_url
    assert "[REDACTED_PASSWORD]" in clean_url

    # 4. Alpaca key
    raw_key = "Using paper key PKTEST1234567890ABCDEF for routing"
    clean_key = SecretRedactor.redact_string(raw_key)
    assert "PKTEST1234567890ABCDEF" not in clean_key
    assert "[REDACTED_API_KEY]" in clean_key


def test_secret_redaction_dict():
    payload = {
        "user": "operator_bob",
        "api_key": "sensitive_key_val_12345",
        "nested": {
            "telegram_bot_token": "987654321:abcdefGHIJKLmnopQRSTuvwxYZ123456",
            "normal_field": "safe_value",
        },
        "list_data": ["public", "Bearer secret_bearer_token_xyz123"],
    }
    cleaned = SecretRedactor.redact(payload)
    assert cleaned["user"] == "operator_bob"
    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["nested"]["telegram_bot_token"] == "[REDACTED]"
    assert cleaned["nested"]["normal_field"] == "safe_value"
    assert cleaned["list_data"][1] == "Bearer [REDACTED_TOKEN]"


def test_structured_json_formatter():
    formatter = StructuredJsonFormatter(service_name="test-desk")
    logger = logging.getLogger("test_logger")
    record = logger.makeRecord(
        name="test_logger",
        level=logging.ERROR,
        fn="test_fn.py",
        lno=42,
        msg="Failed auth with key PKTEST1234567890ABCDEF",
        args=(),
        exc_info=None,
    )
    record.component = "RiskEngine"
    record.event_name = "AUTH_FAILURE"
    record.metadata = {"api_key": "topsecret123", "ticker": "AAPL"}

    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["service"] == "test-desk"
    assert data["level"] == "ERROR"
    assert data["component"] == "RiskEngine"
    assert data["event_name"] == "AUTH_FAILURE"
    assert "PKTEST1234567890ABCDEF" not in data["message"]
    assert "[REDACTED_API_KEY]" in data["message"]
    assert data["metadata"]["api_key"] == "[REDACTED]"
    assert data["metadata"]["ticker"] == "AAPL"


def test_alert_router_critical_condition(obs_db):
    sink = InMemoryAlertSink()
    router = AlertRouter(db=obs_db, sinks=[sink], min_severity=AuditSeverity.WARNING)

    # Route critical emergency liquidation alert
    alert = router.route_alert(
        severity=AuditSeverity.CRITICAL,
        event_name="EMERGENCY_LOCK_ENGAGED",
        message="Circuit breaker breached: equity <= $70.00. Key: PKTEST1234567890ABCDEF",
        metadata={"token": "secret_token_123", "equity": 69.50},
        component="RiskEngine",
        run_id="run_20261004_001",
    )

    # 1. Alert payload created and sanitized
    assert alert.severity == "CRITICAL"
    assert alert.event_name == "EMERGENCY_LOCK_ENGAGED"
    assert "PKTEST1234567890ABCDEF" not in alert.message
    assert "[REDACTED_API_KEY]" in alert.message
    assert alert.metadata["token"] == "[REDACTED]"
    assert alert.metadata["equity"] == 69.50

    # 2. Forwarded to sink
    assert len(sink.alerts) == 1
    assert sink.alerts[0].alert_id == alert.alert_id

    # 3. Persisted in SQLite audit_logs
    logs = obs_db.get_audit_logs()
    crit_logs = [l for l in logs if l.event_name == "EMERGENCY_LOCK_ENGAGED"]
    assert len(crit_logs) == 1
    assert crit_logs[0].severity == AuditSeverity.CRITICAL
    assert "PKTEST1234567890ABCDEF" not in crit_logs[0].message


def test_alert_router_severity_filtering(obs_db):
    sink = InMemoryAlertSink()
    router = AlertRouter(db=obs_db, sinks=[sink], min_severity=AuditSeverity.WARNING)

    # INFO alert: recorded in DB, but filtered from sink
    router.route_alert(
        severity=AuditSeverity.INFO,
        event_name="ORDER_ROUTED",
        message="Order routed to paper broker",
    )
    assert len(sink.alerts) == 0

    # WARNING alert: passes threshold to sink
    router.route_alert(
        severity=AuditSeverity.WARNING,
        event_name="SOFT_FREEZE_ACTIVE",
        message="Buy orders halted",
    )
    assert len(sink.alerts) == 1


def test_telegram_alert_sink():
    mock_transport = MagicMock()
    sink = TelegramAlertSink(transport=mock_transport, chat_id=1001)

    alert = AlertPayload(
        alert_id="a1",
        severity="CRITICAL",
        event_name="HARD_LIQUIDATION",
        component="RiskEngine",
        message="Liquidating all positions",
        metadata={"equity": 69.0},
        timestamp="2026-10-04T05:00:00Z",
        run_id="run_101",
    )

    ok = sink.send_alert(alert)
    assert ok is True
    mock_transport.send_message.assert_called_once()
    call_args = mock_transport.send_message.call_args[1]
    assert call_args["chat_id"] == "1001"
    assert "HARD_LIQUIDATION" in call_args["text"]
    assert "CRITICAL" in call_args["text"]


def test_runbook_documentation_coverage():
    runbook_path = Path("docs/runbook.md")
    assert runbook_path.exists()
    content = runbook_path.read_text()

    # Check key invariants
    assert "Paper Trading Only" in content
    assert "Fail Closed" in content
    assert "$100 Sandbox" in content or "$100" in content

    # Check circuit breaker tiers
    assert "Tier 1: Soft Freeze" in content or "Soft Freeze" in content
    assert "Tier 2: Hard Liquidation" in content or "Hard Liquidation" in content
    assert "HALTED.lock" in content

    # Check incident procedures
    assert "Incident 1" in content  # Market data
    assert "Incident 2" in content  # Reconciliation
    assert "Incident 3" in content  # Protection / Watchdog
    assert "Incident 4" in content  # Run lease
    assert "Incident 5" in content  # HITL expiry
