# [14] Add singleton run lease, durable run state, retry policy, and restart recovery

Type: task
Status: resolved
Blocked by: 09, 12

## Summary
Singleton run lease, durable run state, retry policy, and restart recovery

## Context & Spec Reference
- Phase: Phase 2 — Durable Execution and Reconciliation
- Spec ID: P2-06
- Owner: Runtime agent
- Allowed files: src/main.py, src/storage/*, tests/test_run_lifecycle.py, tests/test_e2e.py
- Validation Command: `./.venv/bin/pytest tests/test_run_lifecycle.py tests/test_e2e.py`

## Acceptance Criteria
Concurrent run attempt is rejected; interrupted run resumes/reconciles safely; all outcomes are recorded

## Comments

## Answer

## Validation Evidence
acquire/renew/release_run_lease (atomic BEGIN IMMEDIATE) in db.py; orchestrator runs under lease with startup reconciliation, blocks on unclean state, records FAILED/COMPLETED, always releases. tests/test_run_lifecycle.py (7) + full suite (117) pass. Implemented directly by orchestrator after subagent quota exhaustion.
