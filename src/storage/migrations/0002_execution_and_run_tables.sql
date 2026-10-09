-- Migration 0002: Execution, Run Lifecycle, and Reconciliation Tables (SPEC.md Phase 2)

-- 12. Trading Runs Lifecycle & Leases
CREATE TABLE IF NOT EXISTS trading_runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK(mode IN ('paper', 'simulation', 'backtest')),
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    status TEXT NOT NULL CHECK(status IN ('STARTING', 'RUNNING', 'COMPLETED', 'FAILED', 'INTERRUPTED')),
    config_hash TEXT NOT NULL,
    trigger TEXT NOT NULL,
    error_message TEXT,
    lease_owner TEXT,
    lease_expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trading_runs_status ON trading_runs(status);

-- 13. Durable Order Intents (Pre-submission Risk & Idempotency Boundary)
CREATE TABLE IF NOT EXISTS order_intents (
    intent_id TEXT PRIMARY KEY,
    request_hash TEXT UNIQUE NOT NULL,
    candidate_id TEXT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    target_qty REAL NOT NULL,
    allocated_usd REAL NOT NULL,
    limit_price REAL,
    stop_loss REAL,
    take_profit REAL,
    risk_decision TEXT NOT NULL CHECK(risk_decision IN ('APPROVED', 'REJECTED')),
    risk_reason TEXT,
    idempotency_key TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'CREATED',
        'SUBMITTED',
        'RECONCILED',
        'REJECTED',
        'EXPIRED',
        'UNKNOWN_PENDING_RECONCILIATION'
    )),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (candidate_id) REFERENCES screened_candidates(candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_order_intents_idempotency ON order_intents(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_order_intents_status ON order_intents(status);

-- 14. Broker Submissions (Audit of External Wire Attempts & Outcomes)
CREATE TABLE IF NOT EXISTS broker_submissions (
    submission_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    broker_order_id TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    client_order_id TEXT NOT NULL,
    request_payload_redacted TEXT,
    response_payload_redacted TEXT,
    outcome_classification TEXT NOT NULL CHECK(outcome_classification IN (
        'ACCEPTED',
        'REJECTED',
        'TIMEOUT',
        'UNKNOWN_PENDING_RECONCILIATION',
        'NETWORK_ERROR'
    )),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (intent_id) REFERENCES order_intents(intent_id)
);
CREATE INDEX IF NOT EXISTS idx_broker_subs_intent ON broker_submissions(intent_id);

-- 15. Reconciliation Events & Audit Mismatches
CREATE TABLE IF NOT EXISTS reconciliation_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT,
    local_snapshot_hash TEXT NOT NULL,
    broker_snapshot_hash TEXT NOT NULL,
    mismatches_json TEXT NOT NULL,
    resolution_status TEXT NOT NULL CHECK(resolution_status IN (
        'HEALTHY_MATCH',
        'RESOLVED_RECONCILED',
        'UNRESOLVED_DISCREPANCY',
        'REVERTED_ORPHAN'
    )),
    resolution_notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES trading_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_recon_events_time ON reconciliation_events(created_at);

-- 16. Protection Status & Watchdog Auditing
CREATE TABLE IF NOT EXISTS protection_status (
    protection_id TEXT PRIMARY KEY,
    position_id TEXT,
    order_id TEXT,
    ticker TEXT NOT NULL,
    protection_mode TEXT NOT NULL CHECK(protection_mode IN (
        'NATIVE_BRACKET',
        'WATCHDOG_SOFTWARE',
        'DEGRADED_UNPROTECTED'
    )),
    stop_loss_order_id TEXT,
    take_profit_order_id TEXT,
    watchdog_healthy INTEGER NOT NULL DEFAULT 1,
    last_verified_at TIMESTAMP NOT NULL,
    degradation_reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (position_id) REFERENCES positions(position_id),
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);
CREATE INDEX IF NOT EXISTS idx_prot_status_pos ON protection_status(position_id);

-- 17. Instrument Metadata & Tradability Attributes
CREATE TABLE IF NOT EXISTS instrument_metadata (
    ticker TEXT PRIMARY KEY,
    sector TEXT,
    industry TEXT,
    exchange TEXT NOT NULL,
    tradable INTEGER NOT NULL DEFAULT 1,
    min_lot_size REAL DEFAULT 1.0,
    price_increment REAL DEFAULT 0.01,
    next_earnings_date TIMESTAMP,
    corporate_action_flag INTEGER DEFAULT 0,
    universe_version TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
