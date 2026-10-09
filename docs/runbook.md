# Operational Runbook: Autonomous Stock Exchange Desk

## 1. Overview & Operating Invariants

This operational runbook provides diagnostic and incident recovery procedures for the autonomous stock exchange desk.

### Strict Sandbox Invariants
- **Paper Trading Only**: Real money or external live account access is strictly prohibited (ADR 0001). All broker adapters must run against paper/sandbox endpoints.
- **Fail Closed**: Any stale, missing, malformed, or conflicting data immediately halts new trade entries (ADR 0002).
- **Final Risk Authority**: The deterministic risk engine evaluates and approves every single order. No LLM, screener, or human operator can bypass risk limits.
- **Capital Constraints ($100 Sandbox)**:
  - Total starting equity: $100.00
  - Maximum concurrent slots: 3 (approx. $30.00 / slot)
  - Mandatory uncommitted cash buffer: $10.00
  - Maximum single-trade risk cap: $3.00 (3.0% of portfolio equity)
  - Daily portfolio loss limit: $3.00
  - Weekly portfolio loss limit: $6.00
  - Consecutive losses pause: 3 losing closed trades pauses entry for the session
  - Stop risk cap across portfolio: $10.00 total open risk
  - Tier 1 Soft Freeze Floor: $80.00 total equity
  - Tier 2 Hard Liquidation Floor: $70.00 total equity

---

## 2. Circuit Breaker Tiers & Emergency Controls

### Tier 0: Normal Operation (Equity > $80.00)
- All screening, deliberation, and execution workflows run normally.

### Tier 1: Soft Freeze (Equity <= $80.00 or Operator Engaged)
- **Symptom**: New candidate entries are rejected with reason `SOFT_FREEZE_ACTIVE`. Active positions continue to run to their bracket stop/take-profit targets.
- **Engaging Soft Freeze**:
  - Telegram: Send `/soft_freeze`
  - Dashboard: Click "Soft Freeze (Halt New Buys)"
  - Code: `risk_engine.engage_soft_freeze(actor="operator", reason="...")`
- **Verification**:
  - Query SQLite: `SELECT * FROM system_controls WHERE control_key = 'soft_freeze';`
  - Ensure `enabled = 1`.
- **Releasing Soft Freeze**:
  - Telegram: Send `/resume`
  - Dashboard: Click "Resume Trading (Clear Locks)"
  - Code: `risk_engine.release_soft_freeze(actor="operator", reason="...")`

### Tier 2: Hard Liquidation Floor (Equity <= $70.00 or HALTED.lock Present)
- **Symptom**: All open positions are immediately liquidated with market orders (`CIRCUIT_BREAKER_HALT`), and a persistent `HALTED.lock` file is created. All trading is strictly halted.
- **Emergency Engagement**:
  - Telegram: Send `/emergency_liquidate`
  - Dashboard: Click "Emergency Liquidate All ($70 Floor)"
- **Recovery Procedure**:
  1. Inspect `HALTED.lock` content and timestamp.
  2. Inspect audit logs: `SELECT * FROM audit_logs WHERE severity = 'CRITICAL' ORDER BY timestamp DESC LIMIT 10;`
  3. Verify with paper broker that all positions are closed: `broker.get_positions()`.
  4. Perform post-mortem analysis of the loss driver.
  5. Clear lock file:
     - Telegram: Send `/resume`
     - Dashboard: Click "Resume Trading"
     - Shell: `rm HALTED.lock`

---

## 3. Incident Recovery Procedures

### Incident 1: Stale or Corrupted Market Data & Provider Timeouts
- **Symptoms**: Screener aborts with `FAIL_CLOSED: Insufficient SPY history` or `DATA_UNHEALTHY: Cache expired`.
- **Root Cause**: Upstream provider (e.g. yfinance) network latency, rate limit, or corrupted local parquet/sqlite cache.
- **Resolution**:
  1. Inspect audit logs for `DATA_PIPELINE_ERROR`:
     ```sql
     SELECT * FROM audit_logs WHERE event_name = 'DATA_PIPELINE_ERROR' ORDER BY timestamp DESC LIMIT 5;
     ```
  2. Check data health in the dashboard under the "Data Freshness" tab.
  3. If cache files under `data/cache/` are corrupted or stale, remove them:
     ```bash
     rm -rf data/cache/*.parquet
     ```
  4. Ensure system fails closed: verify no unintended orders were routed during the outage.

### Incident 2: Broker Reconciliation Discrepancies (`UNRESOLVED_DISCREPANCY`)
- **Symptoms**: Run aborts on startup with `Execution blocked: Account state is unreconciled or has unresolved discrepancies (ADR 0002)`.
- **Root Cause**: Desynchronization between local database records and broker open orders/positions (e.g. orphaned position, missing local order, partial fill).
- **Resolution**:
  1. Query latest reconciliation event:
     ```sql
     SELECT * FROM reconciliation_events ORDER BY created_at DESC LIMIT 1;
     ```
  2. Inspect `mismatches_json` field.
  3. If an orphaned broker position exists (broker holds shares not in local DB), investigate manual execution or test remnant on paper broker.
  4. Run `ReconciliationService.reconcile()`:
     ```python
     from src.broker.reconciliation import ReconciliationService
     # Execute reconciliation and verify clean state
     ```
  5. Only when `resolution_status == 'HEALTHY_MATCH'` or discrepancies are cleanly resolved will the risk engine unlock order approvals.

### Incident 3: Degraded Protection & Watchdog Cadence Breaches
- **Symptoms**: New orders blocked with `ENTRY_BLOCKED_PROTECTION_DEGRADED` or audit alert `WATCHDOG_CADENCE_EXCEEDED`.
- **Root Cause**: Broker adapter does not support native bracket orders and software watchdog has not executed within 300 seconds.
- **Resolution**:
  1. Check `protection_status` table in SQLite:
     ```sql
     SELECT * FROM protection_status ORDER BY updated_at DESC LIMIT 5;
     ```
  2. Confirm watchdog is running on its scheduled heartbeat cadence (e.g. 60 seconds).
  3. Execute manual watchdog sweep:
     ```python
     risk_engine.run_bracket_watchdog()
     ```
  4. If broker supports native brackets (e.g. Alpaca paper), verify stop/take-profit leg IDs exist on the remote broker.

### Incident 4: Singleton Run Lease Stale Lock / Process Crash
- **Symptoms**: New scheduled run fails with `Cannot acquire run lease; active run is in progress`.
- **Root Cause**: Previous process crashed or was terminated without clean `release_run_lease`.
- **Resolution**:
  1. Check active run in `trading_runs`:
     ```sql
     SELECT * FROM trading_runs WHERE status = 'RUNNING';
     ```
  2. If the heartbeat is older than `lease_timeout_seconds` (300s), `acquire_run_lease` automatically recovers the stale lease on the next attempt, marking the stale run `INTERRUPTED`.
  3. To force release manually:
     ```sql
     UPDATE trading_runs SET status = 'INTERRUPTED', ended_at = CURRENT_TIMESTAMP, error_message = 'MANUAL_OPERATOR_RELEASE' WHERE status = 'RUNNING';
     ```

### Incident 5: HITL Deliberation Deadlock & Expiry (15-Minute Timeout)
- **Symptoms**: Candidate in `HITL_ESCALATED` status; Telegram card delivered to operator.
- **Rules**:
  - The human operator has exactly 15 minutes (900 seconds) from `verdict.timestamp` to approve or reject.
  - Any approve/reject attempt after 15 minutes fails closed (`TIMED_OUT` / `EXPIRED`).
  - Human approval DOES NOT bypass the risk engine. If risk invariants fail, the order is blocked.
- **Resolution**:
  - If decision window expired: candidate is marked `EXPIRED` and dropped harmlessly.
  - To inspect pending escalations:
    ```sql
    SELECT * FROM committee_verdicts WHERE hitl_status = 'PENDING_TELEGRAM_RESPONSE';
    ```

### Incident 6: Telegram Transport Failures / Unauthorized Senders
- **Symptoms**: Telegram commands ignored or returning `⛔ Unauthorized: Access denied`.
- **Resolution**:
  1. Verify `TELEGRAM_ALLOWED_USER_IDS` in `.env`:
     ```bash
     TELEGRAM_ALLOWED_USER_IDS="12345678,87654321"
     ```
  2. Check audit logs for `UNAUTHORIZED_TELEGRAM_ACCESS`:
     ```sql
     SELECT * FROM audit_logs WHERE event_name = 'UNAUTHORIZED_TELEGRAM_ACCESS' ORDER BY timestamp DESC LIMIT 5;
     ```
  3. If Telegram API returns HTTP 4xx/5xx or timeouts, inspect `TELEGRAM_TRANSPORT_ERROR` in audit logs.

### Incident 7: Accidental Secret Exposure & Redaction
- **Symptoms**: Sensitive keys or tokens logged or leaked.
- **Resolution**:
  1. Verify `SecretRedactor` is active in `StructuredJsonFormatter` and `AlertRouter`.
  2. Scan audit logs for accidental unredacted credentials:
     ```bash
     grep -E "(bot[0-9]+:[a-zA-Z0-9_-]+|PK[A-Za-z0-9]{16})" logs/*.log
     ```
  3. Immediately revoke and regenerate exposed paper/mock credentials in provider portals.

---

## 4. Observability Reference & Log Format

### Structured JSON Log Schema
All application logs are formatted in single-line JSON:
```json
{
  "timestamp": "2026-10-04T05:20:00.000000+00:00",
  "service": "stock-exchange",
  "level": "INFO",
  "logger": "RiskEngine",
  "message": "Order approved within risk limits",
  "component": "RiskEngine",
  "event_name": "ORDER_APPROVED",
  "run_id": "run_20261004_093000",
  "metadata": {
    "ticker": "AAPL",
    "allocated_usd": 28.50,
    "risk_usd": 2.85
  }
}
```

### Alert Severity Reference
- `CRITICAL`: Immediate threat to portfolio floor ($70 hard liquidation, emergency HALTED.lock, irreconcilable broker discrepancy). Dispatches instant alert to operator.
- `ERROR`: Component failure (provider transport timeout, database migration failure, native leg verification failure).
- `WARNING`: Protective entry block (soft freeze engaged, slot capacity full, concentration limit reached, HITL timeout expired).
- `INFO`: Normal operational milestones (order routed, fill reconciled, run lease completed).
