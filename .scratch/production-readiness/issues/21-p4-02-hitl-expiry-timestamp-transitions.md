# [21] Enforce HITL expiry and state transitions with server-side timestamps

Type: task
Status: resolved
Blocked by: 10

## Summary
Enforce HITL expiry and state transitions with server-side timestamps

## Context & Spec Reference
- Phase: Phase 4 — Operator Control Plane and Observability
- Spec ID: P4-02
- Owner: Control-plane agent
- Allowed files: src/llm/*, src/storage/*, tests/test_hitl_expiry.py, tests/test_llm_committee.py
- Validation Command: `./.venv/bin/pytest tests/test_hitl_expiry.py`

## Acceptance Criteria
Approve/reject after expiry fails; only one terminal outcome is possible; risk validation still runs on approval

## Validation Evidence
- Added atomic HITL transition and query methods to `Database` in `src/storage/db.py`:
  - `transition_hitl_verdict(candidate_id, from_status, to_status, responded_at)`: enforces atomic single state transitions via SQL `WHERE hitl_status = ?`.
  - `get_pending_hitl_verdicts()`: retrieves all pending HITL verdicts.
- Implemented `HITLDecisionManager` in `src/llm/hitl.py`:
  - Enforces server-side timestamp comparison against the 15-minute (900s) timeout window.
  - Both `approve` and `reject` calls after expiry fail explicitly and transition verdict to `TIMED_OUT` and candidate to `EXPIRED`.
  - Enforces strict single terminal outcome (once approved, rejected, or timed out, subsequent mutations fail).
  - Risk engine validation (`risk_engine.validate_and_route_order`) is strictly executed on operator approval; risk invariants (soft freeze, max slots, stop risk cap, cash buffer) remain final authority.
  - Added `sweep_expired` for periodic sweeping of pending escalations.
  - Audits all expiry, approval, rejection, and risk-blocked events to SQLite `audit_logs`.
- Exported `HITLDecisionManager` from `src/llm/__init__.py`.
- Added unit tests in `tests/test_hitl_expiry.py`:
  - Verified approval within window succeeds and routes order to broker.
  - Verified approval after expiry fails and transitions to TIMED_OUT/EXPIRED.
  - Verified rejection after expiry fails.
  - Verified only one terminal outcome is possible (no transition possible from terminal state).
  - Verified risk validation still runs on operator approval and halts orders if risk limits breached.
  - Verified batch expiry sweep and audit logging.
- Validation command passed: `./.venv/bin/pytest tests/test_hitl_expiry.py tests/test_llm_committee.py` (10 passed, 0 failed).
- Full suite: 159 passed in 5.12s.
