# [12] Implement broker reconciliation on startup, before a run, and after submission

Type: task
Status: resolved
Blocked by: 11

## Summary
Broker reconciliation on startup, before a run, and after submission

## Context & Spec Reference
- Phase: Phase 2 — Durable Execution and Reconciliation
- Spec ID: P2-04
- Owner: Broker agent
- Allowed files: src/broker/*, src/storage/*, tests/test_reconciliation.py
- Validation Command: `./.venv/bin/pytest tests/test_reconciliation.py`

## Acceptance Criteria
Missing local order, missing broker order, partial fill, changed position, and orphaned broker position are detected and audited

## Validation Evidence
- Implemented `ReconciliationService` (and `ReconciliationEngine`) in `src/broker/reconciliation.py` providing bidirectional reconciliation between local DB state and broker snapshot.
- Supported discrepancy types: `MISSING_LOCAL_ORDER`, `MISSING_BROKER_ORDER`, `PARTIAL_FILL`, `POSITION_MISMATCH`, and `ORPHANED_BROKER_POSITION`.
- Added cryptographic snapshot hashing (SHA-256) of local and broker snapshots for lineage tracking and audit logging.
- Resolved `UNKNOWN_PENDING_RECONCILIATION` order intents: syncing local order, position, and fill records when confirmed filled on broker, or transitioning to `REJECTED` when absent/rejected.
- Recorded events to `reconciliation_events` table in SQLite WAL database.
- Passed 9/9 tests via `./.venv/bin/pytest tests/test_reconciliation.py -v`.
