# [24] Create versioned data manifests with source, retrieval time, universe membership, corporate actions, and assumptions

Type: task
Status: resolved
Blocked by: 01

## Summary
Versioned data manifests with source, retrieval time, universe membership, corporate actions, and assumptions

## Context & Spec Reference
- Phase: Phase 5 — Research Validity and Strategy Evolution
- Spec ID: P5-01
- Owner: Research agent
- Allowed files: src/backtest/*, tests/test_research_manifests.py
- Validation Command: `./.venv/bin/pytest tests/test_research_manifests.py`

## Acceptance Criteria
Every backtest report points to a reproducible manifest and code/config version

## Validation Evidence
- Implemented `DataManifest` in `src/backtest/manifest.py`:
  - Captures `manifest_id`, `version`, `created_at`, `source`, `date_range`, `universe_membership`, `corporate_actions`, `assumptions`, and `code_version` (git commit sha, branch, config).
  - Deterministic SHA-256 DataFrame hashing (`compute_dataframe_hash`) for each ticker in the universe to guarantee exact bit-for-bit data reproducibility.
  - Added `verify_data_integrity` method to detect missing symbols, modified/tampered values, or unexpected tickers.
  - Implemented `save` and `load` for JSON serialization and deserialization.
- Integrated `DataManifest` into `BacktestResult` in `src/backtest/tier1_vectorized.py` and `ReplayReport` in `src/backtest/tier2_replay.py`:
  - Both vectorized and event-driven backtesting engines generate and link reproducible manifests automatically or accept explicit manifests with data integrity verification.
  - Reports expose `manifest_id`, `manifest`, and `code_version`.
- Added unit tests in `tests/test_research_manifests.py`:
  - Verified manifest generation with SHA-256 checksums and git versioning.
  - Verified integrity verification detects price tampering and missing symbols.
  - Verified JSON serialization and reloading.
  - Verified Tier 1 and Tier 2 backtest runs attach manifest and code version to reports.
- Validation command passed: `./.venv/bin/pytest tests/test_research_manifests.py tests/test_backtester.py` (8 passed, 0 failed).
- Full suite: 177 passed in 5.06s.
