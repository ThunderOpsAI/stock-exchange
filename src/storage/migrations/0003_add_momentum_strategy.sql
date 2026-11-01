-- 0003_add_momentum_strategy.sql
-- Add MOMENTUM_BREAKOUT to screened_candidates strategy CHECK constraint

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS screened_candidates_new (
    candidate_id TEXT PRIMARY KEY,
    timestamp TIMESTAMP NOT NULL,
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL CHECK(strategy IN ('TREND_PULLBACK', 'MEAN_REVERSION', 'MOMENTUM_BREAKOUT')),
    entry_est REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    risk_r REAL NOT NULL,
    allocated_usd REAL NOT NULL,
    rank_score REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING_DELIBERATION', 'APPROVED', 'VETOED', 'HITL_ESCALATED', 'EXPIRED')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO screened_candidates_new SELECT * FROM screened_candidates;
DROP TABLE screened_candidates;
ALTER TABLE screened_candidates_new RENAME TO screened_candidates;

CREATE INDEX IF NOT EXISTS idx_candidates_status ON screened_candidates(status, timestamp);

PRAGMA foreign_keys = ON;
