# [28] Add paper-trading calibration and attribution reports

Type: task
Status: resolved
Blocked by: 12, 24

## Summary
Paper-trading calibration and attribution reports

## Context & Spec Reference
- Phase: Phase 5 — Research Validity and Strategy Evolution
- Spec ID: P5-05
- Owner: Analytics agent
- Allowed files: src/backtest/*, src/storage/*, tests/test_paper_attribution.py
- Validation Command: `./.venv/bin/pytest tests/test_paper_attribution.py`

## Acceptance Criteria
Report reconciles predicted versus realized fill/return, slippage, risk veto effect, and strategy contribution

## Comments
Implemented `PaperAttributionEngine`, `PaperAttributionReport`, `TradeCalibrationRecord`, `RiskVetoAttributionRecord`, and `StrategyAttribution` in `src/backtest/attribution.py`. Reconciles predicted vs realized fill prices, slippage (USD/bps), and returns; computes counterfactual risk veto value (losses avoided vs missed gains); aggregates strategy contribution metrics; links report to `manifest_id` and git version.

## Answer
Resolved. 3 tests passing in `tests/test_paper_attribution.py`.
