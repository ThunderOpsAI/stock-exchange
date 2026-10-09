# [01] Add project tooling and documented local workflow

Type: task
Status: resolved

## Summary
Add project tooling and documented local workflow

## Context & Spec Reference
- Phase: Phase 0 — Baseline and Delivery Scaffold
- Spec ID: P0-01
- Owner: Delivery agent
- Allowed files: README.md, pyproject.toml, .github/workflows/*, optional config files
- Validation Command: `./.venv/bin/pytest -q`

## Acceptance Criteria
Pinned dependencies; formatter, linter, type checker, and test commands documented; CI runs tests; no secrets

## Comments

## Answer
Resolved by:
1. Configuring `pyproject.toml` with build system (setuptools), pinned dependencies for Python 3.12, testing/linting extras, and ruff/mypy configurations.
2. Creating `.github/workflows/ci.yml` for continuous integration running linting, type checks, and tests.
3. Updating `README.md` documenting local workflow commands (test, lint, format, typecheck), paper-trading safety constraints, and  sandbox invariants.

Validation:
Ran `./.venv/bin/pytest -q` -> 38 passed in 2.27s (Pass).
`git diff --check` -> clean.
Secret scan -> 0 secrets found.

