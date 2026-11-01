# 03 Aggressive Momentum Strategy

Type: task
Status: resolved
Blocked by: 01

## Objective
Implement Strategy Gamma (High-Beta Momentum Breakout & Volume Surge) in `src/screener/screener.py` with relaxed liquidity constraints for fast-moving startups ($5M ADDV, $5 min price, 25 bps spread) targeting weekly moves and outsized gains.

## Comments
Added `StrategyType.MOMENTUM_BREAKOUT` to `src/domain/models.py`, `src/storage/schema.sql`, and `src/storage/migrations/0003_add_momentum_strategy.sql`. Implemented `evaluate_momentum_breakout`, aggressive macro threshold, and ranking in `src/screener/screener.py`. All tests passing.

## Answer
Strategy Gamma implemented and tested. Enables aggressive weekly momentum breakout setups for high-beta and startup equities.

