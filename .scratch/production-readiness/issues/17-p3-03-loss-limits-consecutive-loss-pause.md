# [17] Add daily/weekly loss limits, consecutive-loss pause, and portfolio stop-risk cap

Type: task
Status: resolved
Blocked by: 13

## Summary
Daily/weekly loss limits, consecutive-loss pause, and portfolio stop-risk cap

## Context & Spec Reference
- Phase: Phase 3 — Protection and Portfolio Risk
- Spec ID: P3-03
- Owner: Risk agent
- Allowed files: src/risk/*, tests/test_portfolio_risk.py, tests/test_risk_engine.py
- Validation Command: `./.venv/bin/pytest tests/test_portfolio_risk.py`

## Acceptance Criteria
Each limit has boundary tests; the strictest active limit wins and writes an audit decision

## Validation Evidence
- Implemented `PortfolioRiskLimits` and `PortfolioRiskEvaluator` in `src/risk/portfolio_risk.py`:
  - Daily realized loss limit ($3.00 on $100 capital).
  - Weekly realized loss limit ($6.00 on $100 capital).
  - Consecutive losing trade pause (after 3 consecutive losses).
  - Portfolio aggregate open stop-risk cap ($10.00 across 3 slots).
  - Strictest limit priority selection (Daily loss > Weekly loss > Consecutive loss > Stop-risk cap).
  - Audit logging of every check decision (`PORTFOLIO_RISK_LIMIT_BREACHED` / `PORTFOLIO_RISK_PASSED`).
- Integrated portfolio risk evaluation into `RiskEngine.validate_and_route_order` (Gate 4b).
- Exported `PortfolioRiskLimits` and `PortfolioRiskEvaluator` in `src/risk/__init__.py`.
- Passed all 6 boundary and priority tests in `tests/test_portfolio_risk.py` and all 133 tests in the full suite.
