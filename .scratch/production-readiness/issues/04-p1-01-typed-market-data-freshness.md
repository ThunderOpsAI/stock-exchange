# [04] Add typed market-data result/provenance/freshness models; remove blanket exception fallback

Type: task
Status: resolved
Blocked by: 03

## Summary
Typed market-data models with provenance, freshness checks, removing silent exception fallbacks

## Context & Spec Reference
- Phase: Phase 1 — Safety and Data Integrity
- Spec ID: P1-01
- Owner: Data agent
- Allowed files: src/data/*, src/domain/*, tests/test_data_pipeline.py, tests/test_screener.py
- Validation Command: `./.venv/bin/pytest tests/test_data_pipeline.py tests/test_screener.py`

## Acceptance Criteria
Failed/stale provider returns explicit unhealthy result; no entry can use it; tests cover timeout, malformed payload, stale cache

## Comments

## Answer
Resolved by:
1. Adding typed models in `src/domain/models.py`: `DataFreshness`, `MarketDataHealth`, `DataProvenance`, `MarketDataResult`, and `DataIntegrityError`.
2. Refactoring `src/data/pipeline.py` to eliminate silent blanket `except Exception:` fallbacks; implementing `check_cache_freshness` and `fetch_daily_bars_result` with classified error results.
3. Enforcing ADR 0002 fail-closed semantics: stale cache is never returned on provider failure.
4. Adding 12 unit tests in `tests/test_data_pipeline.py` covering fresh data, stale cache rejection, corrupted cache, missing columns, timeout, and network errors.

Validation:
- Ran `./.venv/bin/pytest tests/test_data_pipeline.py tests/test_screener.py` -> 18 passed in 1.25s (Pass).

