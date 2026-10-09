# [16] Add degraded-protection policy and watchdog cadence health checks

Type: task
Status: resolved
Blocked by: 15

## Summary
Degraded-protection policy and watchdog cadence health checks

## Context & Spec Reference
- Phase: Phase 3 — Protection and Portfolio Risk
- Spec ID: P3-02
- Owner: Risk agent
- Allowed files: src/risk/*, tests/test_risk_engine.py, tests/test_watchdog.py
- Validation Command: `./.venv/bin/pytest tests/test_watchdog.py tests/test_risk_engine.py`

## Acceptance Criteria
An entry is blocked if neither native protection nor an explicitly healthy watchdog is available

## Validation Evidence
- Added watchdog cadence tracking (`is_watchdog_healthy`, `watchdog_max_cadence_seconds`) and persistent heartbeat tracking in `src/risk/engine.py`.
- Enforced degraded-protection policy in `RiskEngine.validate_and_route_order` (Gate 2c, ADR 0002):
  - If broker lacks native bracket protection AND software watchdog has not executed within the cadence threshold, new entries are blocked fail-closed.
  - If any active open position is in `DEGRADED_UNPROTECTED` state or has `watchdog_healthy == 0`, new entries are blocked fail-closed.
- Connected post-fill protection status recording using `BrokerProtectionService.record_entry_protection`.
- Passed 17/17 tests in `tests/test_watchdog.py` and `tests/test_risk_engine.py`, and 127 total tests in full suite.
