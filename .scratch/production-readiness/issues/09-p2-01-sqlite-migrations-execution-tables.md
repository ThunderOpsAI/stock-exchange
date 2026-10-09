# [09] Introduce ordered SQLite migrations and execution/run tables

Type: task
Status: resolved
Blocked by: 03

## Summary
Ordered SQLite migrations and execution/run tables

## Context & Spec Reference
- Phase: Phase 2 — Durable Execution and Reconciliation
- Spec ID: P2-01
- Owner: Storage agent
- Allowed files: src/storage/*, tests/test_storage.py, tests/test_migrations.py
- Validation Command: `./.venv/bin/pytest tests/test_storage.py tests/test_migrations.py`

## Acceptance Criteria
Migration from fresh and existing database works; migrations are idempotent; run/order intent/reconciliation records are queryable

## Comments

## Answer
Resolved by:
1. Creating ordered, idempotent SQLite migration scripts in `src/storage/migrations/`:
   - `0001_initial_schema.sql`: Initial domain tables, WAL mode, foreign keys, and controls view.
   - `0002_execution_and_run_tables.sql`: Added `trading_runs`, `order_intents`, `broker_submissions`, `reconciliation_events`, `protection_status`, and `instrument_metadata`.
2. Implementing `MigrationEngine` in `src/storage/migration_engine.py` with atomic transactional execution, automatic discovery, tracking in `schema_migrations`, and legacy database bootstrapping.
3. Updating `Database` in `src/storage/db.py` to execute migrations on startup and adding query/mutation methods for the execution and reconciliation tables.
4. Keeping canonical `src/storage/schema.sql` synchronized.
5. Authoring `tests/test_migrations.py` covering fresh database migrations, idempotency, legacy bootstrap, and CRUD operations on all new execution tables.

Validation:
- Ran `./.venv/bin/pytest tests/test_storage.py tests/test_migrations.py -v` -> 14 passed in 0.26s (Pass).
- Ran `./.venv/bin/pytest -q` -> 82 passed in 2.76s (Pass).
- `git diff --check` -> clean.

