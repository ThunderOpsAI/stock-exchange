# [20] Implement Telegram transport adapter, webhook/polling loop, sender authorization, and callback acknowledgement

Type: task
Status: resolved
Blocked by: 08, 14

## Summary
Telegram transport adapter, webhook/polling loop, sender authorization, callback acknowledgement

## Context & Spec Reference
- Phase: Phase 4 — Operator Control Plane and Observability
- Spec ID: P4-01
- Owner: Control-plane agent
- Allowed files: src/observability/*, tests/test_telegram_transport.py, tests/test_observability.py
- Validation Command: `./.venv/bin/pytest tests/test_telegram_transport.py`

## Acceptance Criteria
Unauthorized users cannot act; callback replay is harmless; transport errors are audited; tests use mocked HTTP only

## Validation Evidence
- Implemented `TelegramTransportAdapter` in `src/observability/telegram_bot.py`:
  - Methods `send_message`, `answer_callback_query`, `get_updates`, and `set_webhook`.
  - Audits transport errors and API failures to SQLite `audit_logs` (`AuditSeverity.ERROR`, `TELEGRAM_TRANSPORT_ERROR`).
  - Strict mocked HTTP in unit tests.
- Enhanced `TelegramBotHandler`:
  - Configurable `allowed_user_ids` (and `TELEGRAM_ALLOWED_USER_IDS` env var) for sender authorization.
  - Unauthorized senders are blocked with warnings audited to SQLite (`UNAUTHORIZED_TELEGRAM_ACCESS`).
  - Callback acknowledgement (`answer_callback_query`) on every callback event.
  - Replay protection: tracking `processed_callback_ids` and checking existing candidate/verdict status so replayed callbacks are harmless and cannot trigger duplicate orders.
  - Full dispatchers for `/status`, `/positions`, `/journal`, `/soft_freeze` (durable), `/emergency_liquidate`, and `/resume`.
  - `process_update` for webhook payloads and `poll_once` for polling loop.
- Added comprehensive unit tests in `tests/test_telegram_transport.py`:
  - Verified sender authorization (authorized vs unauthorized user blocked and audited).
  - Verified callback replay protection is harmless and causes no duplicate orders.
  - Verified transport error auditing into SQLite database.
  - Verified update processing and polling loop with mocked HTTP.
- Executed validation command `./.venv/bin/pytest tests/test_telegram_transport.py tests/test_observability.py`: 15 passed, 0 failed.
- Full test suite: 153 passed in 4.71s.
