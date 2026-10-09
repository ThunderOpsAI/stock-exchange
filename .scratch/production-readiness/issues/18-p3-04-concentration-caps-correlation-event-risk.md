# [18] Add sector/factor/correlation concentration caps and event-risk exclusions

Type: task
Status: resolved
Blocked by: 07, 13

## Summary
Sector/factor/correlation concentration caps and event-risk exclusions

## Context & Spec Reference
- Phase: Phase 3 — Protection and Portfolio Risk
- Spec ID: P3-04
- Owner: Risk agent
- Allowed files: src/risk/*, src/screener/*, tests/test_concentration_risk.py
- Validation Command: `./.venv/bin/pytest tests/test_concentration_risk.py`

## Acceptance Criteria
Correlated exposure is computed from versioned metadata; excess exposure blocks an entry

## Validation Evidence
- Implemented `ConcentrationRiskLimits` and `ConcentrationRiskManager` in `src/risk/concentration.py`:
  - Sector concentration limit (max 1 position per sector in 3-slot portfolio).
  - Pairwise return correlation ceiling (max 0.85 correlation against open positions).
  - Event risk exclusions: corporate action flag and 7-day earnings announcement blackout.
  - Audit logging of every concentration decision.
- Integrated concentration evaluation into `RiskEngine.validate_and_route_order` (Gate 4a).
- Exported `ConcentrationRiskManager` and `ConcentrationRiskLimits` in `src/risk/__init__.py`.
- Passed all 5 tests in `tests/test_concentration_risk.py` and all 138 tests in the full suite.
