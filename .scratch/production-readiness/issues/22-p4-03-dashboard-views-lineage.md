# [22] Build run health, data freshness, reconciliation, protection status, and decision lineage views

Type: task
Status: resolved
Blocked by: 04, 12, 16

## Summary
Run health, data freshness, reconciliation, protection status, and decision lineage views

## Context & Spec Reference
- Phase: Phase 4 — Operator Control Plane and Observability
- Spec ID: P4-03
- Owner: Dashboard agent
- Allowed files: src/observability/*, tests/test_dashboard_views.py, tests/test_observability.py
- Validation Command: `./.venv/bin/pytest tests/test_dashboard_views.py`

## Acceptance Criteria
Each displayed metric derives from persisted records; no reference/demo values appear as live state

## Validation Evidence
- Overhauled `src/observability/dashboard.py` to derive all displayed metrics strictly from persisted SQLite records:
  - Eliminated hardcoded demo equity curves and reference analyst cases.
  - Implemented modular data extraction helpers:
    - `get_run_health_data`: queries singleton run lifecycle state from `trading_runs` (run ID, status, current phase, trigger source, heartbeat, start/end timestamps, error messages).
    - `get_reconciliation_data`: queries latest bidirectional broker reconciliation event from `reconciliation_events` (local & broker snapshot hashes, resolution status, mismatches).
    - `get_protection_status_data`: queries exit protection state from `protection_status` (native bracket vs software watchdog, verified leg IDs, degradation reasons).
    - `get_data_freshness_data`: queries latest market snapshots from `market_snapshots` (as-of timestamp, calculated age in seconds, technical indicators, bid/ask spread).
    - `get_decision_lineage_data`: binds end-to-end lineage from screened candidate -> 3 LLM agent deliberations (scores, stances, catalysts, risks) -> committee consensus verdict -> HITL status -> routed order details.
  - Organized dashboard layout into 4 modular tabs:
    - Tab 1: Portfolio & Positions + Operator Control Panel (durable soft freeze & emergency liquidation).
    - Tab 2: Candidate Deliberation Lineage.
    - Tab 3: Run Health & Protection Status.
    - Tab 4: Market Data Freshness & Reconciliation Lineage.
- Added comprehensive unit tests in `tests/test_dashboard_views.py`:
  - Verified run health data extracted accurately from persisted runs, returning None when empty.
  - Verified reconciliation event data extracted accurately with cryptographic hashes.
  - Verified active protection statuses and leg IDs extracted accurately.
  - Verified data freshness and age calculation from market snapshots.
  - Verified decision lineage linking candidate, deliberations, verdict, and order.
  - Verified that an empty database reports explicit empty state messages and displays zero demo/reference fallback values.
- Validation command passed: `./.venv/bin/pytest tests/test_dashboard_views.py tests/test_observability.py` (12 passed, 0 failed).
- Full suite: 165 passed in 4.94s.
