# Persistence Schema & Domain Event Store

Type: prototype
Status: resolved
Blocked by: 03, 04 (resolved)

## Question

What SQLite/DuckDB schema and domain models are required to store market ticks, screened candidates, LLM committee deliberation logs (reasoning trace, confidence score, sentiment breakdown), order state transitions, executed fills, and portfolio equity curve snapshots for complete post-trade auditability?

## Answer

Constructed and verified a production-grade SQLite WAL-mode relational event store ([prototypes/storage_schema/schema.sql](file:///Users/Thunderops/Documents/Projects/stock-exchange/prototypes/storage_schema/schema.sql)) with full foreign key constraints and covering indexes:
1. `market_snapshots`: Time-series OHLCV and calculated technical features (RSI14, EMA20, ATR14, RVOL20, spread_bps).
2. `screened_candidates`: Quant candidate pool with strategy tag, target levels, and rank score.
3. `llm_deliberations`: Full audit log of Sentiment, Technical, and Adversarial Risk Officer responses, stances, scores, catalyst/risk JSON arrays, and token costs.
4. `committee_verdicts`: Consensus record with composite score, deadlock detection, and HITL Telegram escalation lifecycle tracking (`PENDING_TELEGRAM_RESPONSE` $\to$ `HUMAN_APPROVED`).
5. `orders` & `fills`: Robust order state machine transitions (`PENDING_RISK_CHECK` through `FILLED`) and fill execution records with broker fee and slippage tracking.
6. `positions` & `portfolio_snapshots`: Real-time portfolio equity curve, cash balances, slot allocations, and circuit breaker tiers (0=Normal, 1=Soft Halt $80, 2=Hard Liquidation $70).

Verified via test runner [prototypes/storage_schema/test_schema.py](file:///Users/Thunderops/Documents/Projects/stock-exchange/prototypes/storage_schema/test_schema.py).

## Comments
