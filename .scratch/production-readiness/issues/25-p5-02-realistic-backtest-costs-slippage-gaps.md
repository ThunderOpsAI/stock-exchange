# [25] Model spread, fees, slippage, partial fills, gaps, delistings, and corporate actions

Type: task
Status: resolved
Blocked by: 24

## Summary
Model spread, fees, slippage, partial fills, gaps, delistings, and corporate actions

## Context & Spec Reference
- Phase: Phase 5 — Research Validity and Strategy Evolution
- Spec ID: P5-02
- Owner: Backtest agent
- Allowed files: src/backtest/*, tests/test_backtest_costs.py, tests/test_backtester.py
- Validation Command: `./.venv/bin/pytest tests/test_backtest_costs.py`

## Acceptance Criteria
Tests show the costs affect results; assumptions are surfaced in reports

## Validation Evidence
- Implemented `CostModelConfig`, `ExecutionCostSummary`, and `ExecutionCostModel` in `src/backtest/costs.py`:
  - Bid/ask half-spread and market impact slippage modeling on entries and exits.
  - Fixed and variable transaction/regulatory fees.
  - Volume participation caps and partial fill rates.
  - Overnight gap fill modeling: when an opening bar gaps through a stop-loss level, the exit price is filled at the opening bar price rather than the stop price (`GAP_STOP`).
  - Corporate actions engine: handles stock splits (shares multiplied, prices/brackets adjusted), cash dividends (credited to cash/pnl), and delistings (mandatory liquidation).
  - Cost summaries (`total_spread_cost_usd`, `total_slippage_cost_usd`, `total_fees_usd`, `total_costs_usd`, `gap_exits_count`, `partial_fills_count`) and configurable assumptions.
- Integrated `ExecutionCostModel` into `Tier1VectorizedBacktester` and `Tier2HistoricalReplayEngine`:
  - `BacktestResult` and `ReplayReport` surface `cost_summary` and `cost_assumptions`.
- Added unit tests in `tests/test_backtest_costs.py`:
  - Verified entry and exit execution price and fee mathematics.
  - Verified overnight gap-through slippage model.
  - Verified corporate action adjustments (splits, dividends, delistings).
  - Verified realistic costs directly affect backtest performance (lower final equity and lower profit factor compared to frictionless runs).
- Validation command passed: `./.venv/bin/pytest tests/test_backtest_costs.py tests/test_backtester.py` (7 passed, 0 failed).
- Full suite: 181 passed in 5.49s.
