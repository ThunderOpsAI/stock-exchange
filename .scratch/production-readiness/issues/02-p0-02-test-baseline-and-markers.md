# [02] Capture baseline and add a test marker strategy

Type: task
Status: resolved

## Summary
Capture baseline and add a test marker strategy

## Context & Spec Reference
- Phase: Phase 0 — Baseline and Delivery Scaffold
- Spec ID: P0-02
- Owner: Test agent
- Allowed files: pytest.ini, tests/conftest.py, test files
- Validation Command: `./.venv/bin/pytest -v -m 'unit or not integration'`

## Acceptance Criteria
Fast/unit vs integration markers; deterministic test database and lock fixtures; existing behavior preserved

## Comments

## Answer
Resolved by:
1. Adding pytest marker definitions (`unit`, `integration`, `e2e`) and warning filters in `pytest.ini`.
2. Creating `tests/conftest.py` with deterministic test fixtures (`isolated_db`, `isolated_lock`, and root lockfile isolation guard `guard_root_lock`).
3. Marking test suites appropriately with `pytestmark` across unit, integration, and e2e test files.

Validation:
- Ran `./.venv/bin/pytest -v -m 'unit or not integration'` -> 16 passed, 22 deselected in 1.67s (Pass).
- Ran `./.venv/bin/pytest -q` -> 38 passed in 2.26s (Pass).
- `git diff --check` -> clean.

