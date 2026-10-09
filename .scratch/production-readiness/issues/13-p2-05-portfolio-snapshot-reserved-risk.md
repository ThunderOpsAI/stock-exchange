# [13] Require reconciled portfolio snapshot and reserve risk for pending/unknown orders

Type: task
Status: resolved
Blocked by: 11, 12

## Summary
Reconciled portfolio snapshot and reserve risk for pending/unknown orders

## Context & Spec Reference
- Phase: Phase 2 — Durable Execution and Reconciliation
- Spec ID: P2-05
- Owner: Risk agent
- Allowed files: src/risk/*, tests/test_risk_engine.py
- Validation Command: `./.venv/bin/pytest tests/test_risk_engine.py`

## Acceptance Criteria
An unreconciled account or pending unknown order blocks new entries

## Comments

## Answer

## Validation Evidence
RiskEngine.check_reconciliation_and_pending, reserved_cash, pending-intent slot/ticker/cash reservation (src/risk/engine.py). require_reconciled defaults True (fail closed); tier2 replay with simulated broker opts out. Full suite: 110 passed.
