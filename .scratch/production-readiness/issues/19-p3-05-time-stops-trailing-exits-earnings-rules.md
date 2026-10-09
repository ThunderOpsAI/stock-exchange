# [19] Add time stops, trailing/partial exit policy, and explicit overnight/earnings rules

Type: task
Status: resolved
Blocked by: 15

## Summary
Time stops, trailing/partial exit policy, explicit overnight/earnings rules

## Context & Spec Reference
- Phase: Phase 3 — Protection and Portfolio Risk
- Spec ID: P3-05
- Owner: Strategy agent
- Allowed files: src/risk/*, src/screener/*, tests/test_exit_policy.py
- Validation Command: `./.venv/bin/pytest tests/test_exit_policy.py`

## Acceptance Criteria
Exit policy is deterministic, persisted, and tested against gaps and missing quotes

## Validation Evidence
- Implemented `ExitPolicyConfig` and `ExitPolicyManager` in `src/risk/exit_policy.py`:
  - Time stop (holding period >= 10 days triggers `TIME_STOP`).
  - Trailing stop (activated at +5% peak gain, trails 3% behind high-water mark; triggers `TRAILING_STOP`).
  - Pre-earnings mandatory liquidation (within 24h of earnings announcement triggers `EARNINGS_PRE_EXIT`).
  - Overnight gap stop detection (price opening below stop-loss triggers `GAP_STOP`).
  - Missing/invalid quote handling (fails closed without unintended market orders).
  - Persistence of exit reasons and realized PnL in database positions and audit logs.
- Integrated `ExitPolicyManager` into `RiskEngine.run_bracket_watchdog`.
- Added `TRAILING_STOP` and `EARNINGS_PRE_EXIT` to `ExitReason` enum in `src/domain/models.py`.
- Passed all 6 tests in `tests/test_exit_policy.py` and 144 tests in the full suite.
