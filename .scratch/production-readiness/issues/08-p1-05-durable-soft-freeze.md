# [08] Add a durable, persistent soft-freeze state to risk evaluation

Type: task
Status: resolved
Blocked by: 03

## Summary
Durable, persistent soft-freeze state in risk evaluation

## Context & Spec Reference
- Phase: Phase 1 — Safety and Data Integrity
- Spec ID: P1-05
- Owner: Risk agent
- Allowed files: src/risk/*, src/storage/*, tests/test_risk_engine.py
- Validation Command: `./.venv/bin/pytest tests/test_risk_engine.py`

## Acceptance Criteria
Soft freeze survives restart; Telegram/dashboard controls write state; risk validation blocks entries while active

## Comments

## Answer
Resolved by:
1. Adding `system_controls` table and `operator_controls` view/triggers in `src/storage/schema.sql`.
2. Implementing `set_soft_freeze` and `is_soft_freeze_active` in `src/storage/db.py`.
3. Updating `src/risk/engine.py` with `engage_soft_freeze`, `release_soft_freeze`, and persistent soft-freeze checks in `evaluate_circuit_breaker`, `calculate_position_size`, and `validate_and_route_order`.
4. Adding test cases in `tests/test_risk_engine.py` verifying engagement, rejection of entries during soft freeze, and persistence across engine and DB reinstantiations.

Validation:
- Ran `./.venv/bin/pytest tests/test_risk_engine.py` -> 7 passed in 0.28s (Pass).

