# [23] Add structured logs, alert routing interfaces, redaction, and operational runbook

Type: task
Status: resolved
Blocked by: 14

## Summary
Structured logs, alert routing interfaces, redaction, operational runbook

## Context & Spec Reference
- Phase: Phase 4 — Operator Control Plane and Observability
- Spec ID: P4-04
- Owner: Observability agent
- Allowed files: src/observability/*, docs/runbook.md, tests/test_structured_logging.py
- Validation Command: `./.venv/bin/pytest tests/test_structured_logging.py`

## Acceptance Criteria
Critical conditions create an audit event and alert payload; secrets are redacted; runbook covers incident recovery

## Validation Evidence
- Implemented `src/observability/logging.py`:
  - `SecretRedactor`: regex-based and recursive dictionary/list redaction for Telegram bot tokens, Bearer tokens, API keys, passwords in URLs, and Alpaca/AWS-style credentials.
  - `StructuredJsonFormatter`: formats standard Python logging records into single-line JSON with sanitized messages, components, event names, and metadata.
  - `AlertPayload`, `AlertSink`, `InMemoryAlertSink`, `TelegramAlertSink`, and `AlertRouter`:
    - Automatically redacts sensitive fields before emission.
    - Writes audit record to SQLite database.
    - Routes `AlertPayload` to sinks based on severity thresholds (`AuditSeverity.WARNING`, `ERROR`, `CRITICAL`).
- Exported logging classes from `src/observability/__init__.py`.
- Authored comprehensive operational runbook in `docs/runbook.md`:
  - Operating principles & sandbox invariants ($100 sandbox, $10 cash buffer, $3 max risk, 3 slots).
  - Circuit Breaker procedures for Tier 0 (Normal), Tier 1 (Soft Freeze), and Tier 2 (Hard Liquidation with HALTED.lock).
  - Detailed diagnostic and recovery procedures for 7 operational incidents (market data failure, broker reconciliation discrepancies, degraded exit protection, singleton lease recovery, HITL deadlocks & expiry, Telegram transport failures, secret redaction).
  - Observability reference and structured JSON log schema.
- Added comprehensive unit tests in `tests/test_structured_logging.py`:
  - Verified string and dict secret redaction.
  - Verified structured JSON logging formatter produces valid redacted JSON records.
  - Verified critical condition alert routing creates SQLite audit event and dispatches alert payload to sinks.
  - Verified severity filtering (INFO filtered from sink, WARNING/CRITICAL dispatched).
  - Verified Telegram alert sink formatting.
  - Verified runbook coverage.
- Validation command passed: `./.venv/bin/pytest tests/test_structured_logging.py` (7 passed, 0 failed).
- Full suite: 172 passed in 4.82s.
