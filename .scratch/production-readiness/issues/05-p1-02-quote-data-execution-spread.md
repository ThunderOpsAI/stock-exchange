# [05] Separate daily historical bars from execution quote data, with bid/ask and timestamps

Type: task
Status: resolved
Blocked by: 04

## Summary
Separate daily historical bars from execution quote data, bid/ask and timestamps

## Context & Spec Reference
- Phase: Phase 1 — Safety and Data Integrity
- Spec ID: P1-02
- Owner: Data agent
- Allowed files: src/data/*, src/screener/*, tests/test_data_pipeline.py, tests/test_screener.py
- Validation Command: `./.venv/bin/pytest tests/test_data_pipeline.py tests/test_screener.py`

## Acceptance Criteria
Entry pricing and spread checks use quote timestamp and bid/ask; estimated spread is never used for execution

## Comments

## Answer
Resolved by:
1. Defining `ExecutionQuote` model in `src/domain/models.py` with bid/ask, timestamp, spread_bps, and spread_rel calculations and validation against non-positive bids and crossed quotes.
2. Adding execution quote fetch and synthesis interfaces to `src/data/pipeline.py` (`fetch_execution_quote`, `inject_execution_quote`, etc.), completely decoupling execution quotes from daily bars.
3. Updating `QuantitativeScreener` in `src/screener/screener.py` to use verified execution quotes for spread threshold checks (<= 6 bps) and calculating entry pricing (`entry_est`) from ask prices for buy orders.
4. Adding tests in `tests/test_data_pipeline.py` and `tests/test_screener.py` validating quote integrity, spread rejection, ask-based entry pricing, and quote-aware universe scanning.

Validation:
- Ran `./.venv/bin/pytest tests/test_data_pipeline.py tests/test_screener.py` -> 25 passed in 1.23s (Pass).
- Ran `./.venv/bin/pytest -q` -> 60 passed in 2.08s (Pass).
- `git diff --check` -> clean.

