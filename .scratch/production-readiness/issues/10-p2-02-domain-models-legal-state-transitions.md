# [10] Add models and legal state transitions for run, order intent, submission, reconciliation, and degraded protection

Type: task
Status: resolved
Blocked by: 09

## Summary
Models and legal state transitions for run, order intent, submission, reconciliation, and degraded protection

## Context & Spec Reference
- Phase: Phase 2 — Durable Execution and Reconciliation
- Spec ID: P2-02
- Owner: Domain agent
- Allowed files: src/domain/*, tests/test_domain_models.py
- Validation Command: `./.venv/bin/pytest tests/test_domain_models.py`

## Acceptance Criteria
Invalid transitions are rejected; all states have focused unit tests

## Comments

## Answer
Resolved by:
1. Adding domain models in `src/domain/models.py`: `TradingRun`, `OrderIntent`, `BrokerSubmission`, `ReconciliationRecord`, `ProtectionStatusRecord`, `InstrumentMetadata`, and enums `TradingRunStatus`, `OrderIntentStatus`, `BrokerSubmissionOutcome`, `ReconciliationStatus`, `ProtectionMode`.
2. Defining explicit state transition rules and methods (`transition_to`, `can_transition_to`) with `IllegalStateTransitionError` for illegal attempts.
3. Adding degraded protection detection logic in `ProtectionStatusRecord.is_degraded()`.
4. Adding comprehensive unit tests in `tests/test_domain_models.py` validating run lifecycle, intent lifecycle, timeout transitions to `UNKNOWN_PENDING_RECONCILIATION`, illegal jumps, and protection degradation.

Validation:
- Ran `./.venv/bin/pytest tests/test_domain_models.py -v` -> 8 passed in 0.06s (Pass).
- Ran `./.venv/bin/pytest -q` -> 90 passed in 2.74s (Pass).
- `git diff --check` -> clean.

