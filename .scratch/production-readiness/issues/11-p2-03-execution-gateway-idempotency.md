# [11] Build an ExecutionGateway around paper brokers with idempotency and classified retry outcomes

Type: task
Status: resolved
Blocked by: 09, 10

## Summary
ExecutionGateway around paper brokers with idempotency and classified retry outcomes

## Context & Spec Reference
- Phase: Phase 2 — Durable Execution and Reconciliation
- Spec ID: P2-03
- Owner: Broker agent
- Allowed files: src/broker/*, tests/test_broker_adapters.py, tests/test_execution_gateway.py
- Validation Command: `./.venv/bin/pytest tests/test_execution_gateway.py tests/test_broker_adapters.py`

## Acceptance Criteria
Repeated submission with same intent creates at most one broker order; timeout produces UNKNOWN_PENDING_RECONCILIATION, never a blind retry

## Validation Evidence
- Implemented `ExecutionGateway` in `src/broker/gateway.py` wrapping paper broker adapters with persistent `order_intents` and `broker_submissions` records in SQLite.
- Enforced paper-trading only check (rejects any `live` broker mode).
- Verified idempotent duplicate submissions return existing order without creating secondary broker orders.
- Verified timeout/network errors record UNKNOWN_PENDING_RECONCILIATION and prevent duplicate submissions until reconciled.
- Passed 12/12 tests via `./.venv/bin/pytest tests/test_execution_gateway.py tests/test_broker_adapters.py -v`.
