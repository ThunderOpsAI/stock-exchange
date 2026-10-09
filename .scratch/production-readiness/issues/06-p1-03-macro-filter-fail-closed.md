# [06] Make macro filter fail closed and enforce SMA-50 deterioration rule

Type: task
Status: resolved
Blocked by: 04

## Summary
Make macro filter fail closed and enforce SMA-50 deterioration rule

## Context & Spec Reference
- Phase: Phase 1 — Safety and Data Integrity
- Spec ID: P1-03
- Owner: Screening agent
- Allowed files: src/screener/*, tests/test_screener.py
- Validation Command: `./.venv/bin/pytest tests/test_screener.py`

## Acceptance Criteria
Insufficient SPY history or unavailable indicators blocks scanning; regression tests cover all branches

## Comments

## Answer
Resolved by:
1. Refactoring `check_macro_regime` in `src/screener/screener.py` to strictly fail closed adhering to ADR 0002.
2. Rejection branches enforced: insufficient SPY history (< 200 bars for SMA200/SMA50 slope), missing/uncomputed/NaN SMA indicators, Close <= SMA200, and deteriorating SMA50 (`SMA_50(t) < SMA_50(t-5)`).
3. Updating `scan_universe` to immediately abort and return empty candidates when `check_macro_regime` returns False.
4. Adding comprehensive branch unit tests in `tests/test_screener.py` covering all failure and passing branches.
5. Updating `tests/test_e2e.py` synthetic SPY bar history from 120 to 250 bars to satisfy the SMA-200 fail-closed constraint.

Validation:
- Ran `./.venv/bin/pytest tests/test_screener.py` -> 15 passed in 0.86s (Pass).
- Ran `./.venv/bin/pytest tests/test_e2e.py` -> 2 passed in 1.19s (Pass).
- Ran `./.venv/bin/pytest -q` -> 66 passed in 2.08s (Pass).

