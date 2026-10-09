# [15] Describe broker protection capabilities and create/verify native bracket protection for supported paper brokers

Type: task
Status: resolved
Blocked by: 11

## Summary
Broker protection capabilities and native bracket protection for supported paper brokers

## Context & Spec Reference
- Phase: Phase 3 — Protection and Portfolio Risk
- Spec ID: P3-01
- Owner: Broker agent
- Allowed files: src/broker/*, tests/test_broker_protection.py
- Validation Command: `./.venv/bin/pytest tests/test_broker_protection.py`

## Acceptance Criteria
Order records identify native vs watchdog protection; native leg IDs are reconciled

## Validation Evidence
- Added capability descriptor `supports_native_bracket` to `AbstractBrokerAdapter` (default `False`), `SimulatedPaperBroker` (`True`), `AlpacaPaperBroker` (`True`), and `EtoroBrokerAdapter` (`False`).
- Implemented `BrokerProtectionService` in `src/broker/protection.py`:
  - Records protection mode (`NATIVE_BRACKET`, `WATCHDOG_SOFTWARE`, or `DEGRADED_UNPROTECTED`).
  - Persists protection records into `protection_status` SQLite table.
  - Verifies native leg order IDs against broker open orders (`verify_native_legs`).
  - Automatically degrades to `DEGRADED_UNPROTECTED` and alerts if native legs are missing/cancelled.
- Exported `BrokerProtectionService` in `src/broker/__init__.py`.
- Passed all 5 tests in `tests/test_broker_protection.py` and full suite of 122 tests.
