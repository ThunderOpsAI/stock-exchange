# [31] Measure heuristic/model calibration, disagreement, override results, and veto precision

Type: task
Status: resolved
Blocked by: 28, 29

## Summary
Measure heuristic/model calibration, disagreement, override results, veto precision

## Context & Spec Reference
- Phase: Phase 6 — Decision Intelligence (Optional)
- Spec ID: P6-03
- Owner: Analytics agent
- Allowed files: src/llm/*, src/backtest/*, tests/test_committee_calibration.py
- Validation Command: `./.venv/bin/pytest tests/test_committee_calibration.py`

## Acceptance Criteria
Dashboard/report shows decision quality; no metric alone changes risk limits automatically

## Comments
Implemented `CommitteeCalibrationEngine` and `CommitteeDecisionQualityReport` in `src/llm/calibration.py`. Quantifies persona calibration (correlation with realized PnL), pairwise stance disagreement rates, risk officer veto precision (losses avoided vs missed gains), and human operator override efficacy. Enforces strict safety boundary invariant: decision metrics are purely observational and can never automatically adjust or override deterministic risk engine limits.

## Answer
Resolved. Tests passing in `tests/test_committee_calibration.py`.
