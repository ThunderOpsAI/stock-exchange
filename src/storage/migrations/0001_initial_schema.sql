-- Migration 0001: Initial Relational Schema & Event Store

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- 1. Daily Market Snapshot & Quantitative Features
CREATE TABLE IF NOT EXISTS market_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP NOT NULL,
    ticker TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    rsi_14 REAL,
    ema_20 REAL,
    sma_50 REAL,
    sma_200 REAL,
    atr_14 REAL,
    rs_spy_63d REAL,
    rvol_20 REAL,
    spread_bps REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_market_snap_ticker_time ON market_snapshots(ticker, timestamp);

-- 2. Screened Quantitative Candidates
CREATE TABLE IF NOT EXISTS screened_candidates (
    candidate_id TEXT PRIMARY KEY,
    timestamp TIMESTAMP NOT NULL,
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL CHECK(strategy IN ('TREND_PULLBACK', 'MEAN_REVERSION')),
    entry_est REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    risk_r REAL NOT NULL,
    allocated_usd REAL NOT NULL,
    rank_score REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING_DELIBERATION', 'APPROVED', 'VETOED', 'HITL_ESCALATED', 'EXPIRED')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON screened_candidates(status, timestamp);

-- 3. LLM Agent Committee Deliberation Logs
CREATE TABLE IF NOT EXISTS llm_deliberations (
    deliberation_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    agent_role TEXT NOT NULL CHECK(agent_role IN ('sentiment_catalyst', 'technical_structure', 'adversarial_risk')),
    model_name TEXT NOT NULL,
    stance TEXT NOT NULL CHECK(stance IN ('BULLISH', 'NEUTRAL', 'BEARISH', 'VETO')),
    score_10 REAL NOT NULL,
    bullish_catalysts_json TEXT NOT NULL,
    risk_factors_json TEXT NOT NULL,
    rationale_summary TEXT NOT NULL,
    token_cost_usd REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (candidate_id) REFERENCES screened_candidates(candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_deliberations_candidate ON llm_deliberations(candidate_id);

-- 4. Committee Consensus & HITL Escalation Records
CREATE TABLE IF NOT EXISTS committee_verdicts (
    verdict_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    composite_score REAL NOT NULL,
    verdict_outcome TEXT NOT NULL CHECK(verdict_outcome IN ('AUTO_APPROVED', 'HITL_ESCALATED', 'AUTO_DROPPED')),
    risk_officer_dissent INTEGER NOT NULL DEFAULT 0,
    hitl_status TEXT CHECK(hitl_status IN ('PENDING_TELEGRAM_RESPONSE', 'HUMAN_APPROVED', 'HUMAN_REJECTED', 'TIMED_OUT')),
    hitl_responded_at TIMESTAMP,
    telegram_message_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (candidate_id) REFERENCES screened_candidates(candidate_id)
);

-- 5. Order State Machine
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    client_order_id TEXT UNIQUE NOT NULL,
    candidate_id TEXT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    order_type TEXT NOT NULL CHECK(order_type IN ('MARKET', 'LIMIT', 'BRACKET')),
    allocated_usd REAL NOT NULL,
    target_qty REAL NOT NULL,
    stop_loss REAL,
    take_profit REAL,
    state TEXT NOT NULL CHECK(state IN (
        'PENDING_RISK_CHECK',
        'RISK_APPROVED',
        'RISK_REJECTED',
        'ROUTED_TO_BROKER',
        'FILLED',
        'PARTIALLY_FILLED',
        'CANCELLED',
        'REJECTED_BY_BROKER',
        'EXPIRED'
    )),
    rejection_reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (candidate_id) REFERENCES screened_candidates(candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state);

-- 6. Executed Fills & Transaction Accounting
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    broker_order_id TEXT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    filled_qty REAL NOT NULL,
    filled_price REAL NOT NULL,
    filled_notional REAL NOT NULL,
    broker_fee_usd REAL DEFAULT 0.0,
    slippage_usd REAL DEFAULT 0.0,
    executed_at TIMESTAMP NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

-- 7. Active & Closed Positions
CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    broker_position_id TEXT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    current_price REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    market_value REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('OPEN', 'CLOSED')),
    opened_at TIMESTAMP NOT NULL,
    closed_at TIMESTAMP,
    realized_pnl REAL DEFAULT 0.0,
    exit_reason TEXT CHECK(exit_reason IN ('TAKE_PROFIT', 'STOP_LOSS', 'GAP_STOP', 'TIME_STOP', 'CIRCUIT_BREAKER_HALT', 'MANUAL_CLOSE'))
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

-- 8. Portfolio Snapshots & Equity Curve (Every Bar / Hour)
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP NOT NULL,
    total_equity REAL NOT NULL,
    cash_balance REAL NOT NULL,
    invested_capital REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    active_slots_used INTEGER NOT NULL,
    circuit_breaker_tier INTEGER NOT NULL DEFAULT 0 CHECK(circuit_breaker_tier IN (0, 1, 2)),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_portfolio_snap_time ON portfolio_snapshots(timestamp);

-- 9. System Audit Event Log
CREATE TABLE IF NOT EXISTS system_audit_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    component TEXT NOT NULL,
    event_name TEXT NOT NULL,
    message TEXT NOT NULL,
    metadata_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 10. LLM Deliberation Cache
CREATE TABLE IF NOT EXISTS deliberation_cache (
    cache_key TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    strategy_id TEXT,
    agent_role TEXT NOT NULL,
    prompt_hash TEXT,
    model_name TEXT,
    response_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_delib_cache_sym_date ON deliberation_cache(symbol, as_of_date, agent_role);

-- 11. Persistent System & Operator Controls
CREATE TABLE IF NOT EXISTS system_controls (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE VIEW IF NOT EXISTS operator_controls AS SELECT * FROM system_controls;

CREATE TRIGGER IF NOT EXISTS trg_operator_controls_insert
INSTEAD OF INSERT ON operator_controls
BEGIN
    INSERT OR REPLACE INTO system_controls (key, value, actor, reason, updated_at)
    VALUES (NEW.key, NEW.value, NEW.actor, NEW.reason, NEW.updated_at);
END;

CREATE TRIGGER IF NOT EXISTS trg_operator_controls_update
INSTEAD OF UPDATE ON operator_controls
BEGIN
    UPDATE system_controls
    SET value = NEW.value, actor = NEW.actor, reason = NEW.reason, updated_at = NEW.updated_at
    WHERE key = OLD.key;
END;

CREATE TRIGGER IF NOT EXISTS trg_operator_controls_delete
INSTEAD OF DELETE ON operator_controls
BEGIN
    DELETE FROM system_controls WHERE key = OLD.key;
END;
