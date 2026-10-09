# [07] Add market calendar, session, halt, and corporate-action entry gates

Type: task
Status: resolved
Blocked by: 04

## Summary
Add market calendar, session, halt, and corporate-action entry gates

## Context & Spec Reference
- Phase: Phase 1 — Safety and Data Integrity
- Spec ID: P1-04
- Owner: Runtime agent
- Allowed files: src/data/*, src/screener/*, src/domain/*, tests/test_market_calendar.py
- Validation Command: `./.venv/bin/pytest tests/test_market_calendar.py`

## Acceptance Criteria
Closed/unknown market status, a halted symbol, or corporate-action risk blocks a new order with an audit record

## Comments

## Answer
Resolved by:
1. Adding `MarketSessionStatus` and `GateCheckResult` models in `src/domain/models.py`.
2. Implementing `MarketCalendarGateService` in `src/data/calendar.py` with regular trading hours checks (09:30 - 16:00 ET, Monday-Friday), NYSE observed holidays calendar, symbol trading halt registry, and corporate action / earnings blackout window enforcement (< 7 days).
3. Integrating entry gates into `QuantitativeScreener.scan_universe` in `src/screener/screener.py` to filter out halted or blacked-out candidates.
4. Adding tests in `tests/test_market_calendar.py` validating regular hours, weekend closures, pre/post-market closures, holidays, unknown session fail-closed, symbol halts, earnings blackouts, and screener integration.

Validation:
- Ran `./.venv/bin/pytest tests/test_market_calendar.py -v` -> 9 passed in 1.03s (Pass).
- Ran `./.venv/bin/pytest -q` -> 75 passed in 2.28s (Pass).
- `git diff --check` -> clean.

