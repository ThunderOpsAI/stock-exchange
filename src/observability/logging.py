"""
Structured JSON Logging, Secret Redaction, and Alert Routing Interface.
Implements:
- SecretRedactor: Redacts API keys, tokens, credentials, and passwords from logs and payloads.
- StructuredJsonFormatter: Standard Python logging formatter that produces clean, redacted JSON records.
- AlertRouter & AlertSink: Protocol and router for critical condition alerting with database audit trails.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Union

from src.domain.models import AuditSeverity
from src.storage.db import Database

# Sensitive key patterns for dictionary keys
SENSITIVE_KEY_NAMES = {
    "api_key",
    "apikey",
    "secret",
    "secret_key",
    "token",
    "bot_token",
    "access_token",
    "auth_token",
    "password",
    "passwd",
    "private_key",
    "credentials",
    "authorization",
}

# Regex patterns for raw string sensitive data
REDACTION_PATTERNS = [
    # Telegram Bot Token: 123456789:ABCdefGHIjklMNOpqrsTUVwxyz-12345
    (re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{20,50}\b"), "[REDACTED_TELEGRAM_TOKEN]"),
    # Bearer tokens
    (re.compile(r"(?i)\bbearer\s+([A-Za-z0-9_\-\.]{16,})\b"), "Bearer [REDACTED_TOKEN]"),
    # URL credentials: http://user:pass@host
    (re.compile(r"://([^:/\s]+):([^@/\s]+)@"), r"://\1:[REDACTED_PASSWORD]@"),
    # Key-value secret assignments: api_key="secret", token=abc12345
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|auth|bot_token)\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.]{8,})['\"]?"
        ),
        r"\1=[REDACTED]",
    ),
    # Alpaca / AWS style keys: PK..., AK..., SK... followed by 16+ chars
    (re.compile(r"\b(PK|CK|AK|SK)[A-Za-z0-9]{16,}\b"), "[REDACTED_API_KEY]"),
    # Google AI / Gemini API keys: AIza...
    (re.compile(r"\bAIza[0-9A-Za-z-_]{30,50}\b"), "[REDACTED_GEMINI_KEY]"),
]


class SecretRedactor:
    """Utility class to sanitize sensitive strings, dictionaries, lists, and log records."""

    @staticmethod
    def redact_string(text: str) -> str:
        if not isinstance(text, str):
            return text
        redacted = text
        for pattern, replacement in REDACTION_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted

    @classmethod
    def redact(cls, data: Any) -> Any:
        if isinstance(data, str):
            return cls.redact_string(data)
        elif isinstance(data, dict):
            clean_dict: Dict[str, Any] = {}
            for k, v in data.items():
                k_lower = str(k).lower().strip()
                if any(sensitive in k_lower for sensitive in SENSITIVE_KEY_NAMES):
                    clean_dict[k] = "[REDACTED]"
                else:
                    clean_dict[k] = cls.redact(v)
            return clean_dict
        elif isinstance(data, (list, tuple, set)):
            clean_list = [cls.redact(item) for item in data]
            return clean_list if not isinstance(data, tuple) else tuple(clean_list)
        return data


class StructuredJsonFormatter(logging.Formatter):
    """
    Formats standard Python logging records into single-line JSON with automatic secret redaction.
    """

    def __init__(self, service_name: str = "stock-exchange", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        # Base structured fields
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "service": self.service_name,
            "level": record.levelname,
            "logger": record.name,
            "message": SecretRedactor.redact_string(record.getMessage()),
            "component": getattr(record, "component", record.name),
            "event_name": getattr(record, "event_name", None),
            "run_id": getattr(record, "run_id", None),
        }

        # Include custom metadata if present
        extra_metadata = getattr(record, "metadata", None)
        if extra_metadata and isinstance(extra_metadata, dict):
            log_entry["metadata"] = SecretRedactor.redact(extra_metadata)

        # Exception information
        if record.exc_info:
            log_entry["exception"] = SecretRedactor.redact_string(
                self.formatException(record.exc_info)
            )

        return json.dumps(log_entry)


@dataclass
class AlertPayload:
    alert_id: str
    severity: str
    event_name: str
    component: str
    message: str
    metadata: Dict[str, Any]
    timestamp: str
    run_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AlertSink(Protocol):
    """Protocol representing a destination for system alerts."""

    def send_alert(self, alert: AlertPayload) -> bool:
        ...


class InMemoryAlertSink:
    """In-memory alert sink for testing and inspection."""

    def __init__(self):
        self.alerts: List[AlertPayload] = []

    def send_alert(self, alert: AlertPayload) -> bool:
        self.alerts.append(alert)
        return True

    def clear(self) -> None:
        self.alerts.clear()


class TelegramAlertSink:
    """Dispatches critical alerts to operator via Telegram Transport Adapter."""

    def __init__(self, transport: Any, chat_id: Union[str, int]):
        self.transport = transport
        self.chat_id = str(chat_id)

    def send_alert(self, alert: AlertPayload) -> bool:
        try:
            icon = "🚨" if alert.severity == "CRITICAL" else "⚠️"
            text = (
                f"{icon} *SYSTEM ALERT [{alert.severity}]*\n"
                f"• Event: `{alert.event_name}`\n"
                f"• Component: `{alert.component}`\n"
                f"• Time: `{alert.timestamp}`\n"
                f"• Message: {alert.message}\n"
            )
            if alert.run_id:
                text += f"• Run ID: `{alert.run_id}`\n"
            if alert.metadata:
                text += f"```json\n{json.dumps(alert.metadata, indent=2)}\n```"
            self.transport.send_message(chat_id=self.chat_id, text=text)
            return True
        except Exception:
            return False


class AlertRouter:
    """
    Central alert router.
    - Sanitizes sensitive secrets before emission.
    - Writes audit record into SQLite database.
    - Emits structured AlertPayload to registered sinks when severity meets threshold.
    """

    SEVERITY_ORDER = {
        AuditSeverity.INFO: 1,
        AuditSeverity.WARNING: 2,
        AuditSeverity.ERROR: 3,
        AuditSeverity.CRITICAL: 4,
    }

    def __init__(
        self,
        db: Optional[Database] = None,
        sinks: Optional[List[AlertSink]] = None,
        min_severity: AuditSeverity = AuditSeverity.WARNING,
    ):
        self.db = db
        self.sinks: List[AlertSink] = sinks or []
        self.min_severity = min_severity

    def add_sink(self, sink: AlertSink) -> None:
        self.sinks.append(sink)

    def route_alert(
        self,
        severity: AuditSeverity,
        event_name: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
        component: str = "System",
        run_id: Optional[str] = None,
    ) -> AlertPayload:
        """
        Creates and routes an alert.
        Redacts sensitive tokens, writes SQLite audit log, and forwards to sinks.
        """
        clean_message = SecretRedactor.redact_string(message)
        clean_metadata = SecretRedactor.redact(metadata or {})

        now_iso = datetime.now(timezone.utc).isoformat()
        alert = AlertPayload(
            alert_id=f"alert_{uuid.uuid4().hex[:8]}",
            severity=severity.value,
            event_name=event_name,
            component=component,
            message=clean_message,
            metadata=clean_metadata,
            timestamp=now_iso,
            run_id=run_id,
        )

        # 1. Audit trail in database
        if self.db is not None:
            try:
                self.db.save_audit_log(
                    severity=severity,
                    component=component,
                    event_name=event_name,
                    message=clean_message,
                    metadata=clean_metadata,
                )
            except Exception as e:
                logging.getLogger("AlertRouter").error(f"Failed to record audit log: {e}")

        # 2. Forward to sinks if severity threshold met
        if self.SEVERITY_ORDER.get(severity, 0) >= self.SEVERITY_ORDER.get(self.min_severity, 2):
            for sink in self.sinks:
                try:
                    sink.send_alert(alert)
                except Exception as e:
                    logging.getLogger("AlertRouter").error(f"Failed to deliver alert to sink {sink}: {e}")

        return alert
