# 04 Profile Orchestrator Dual Desk

Type: task
Status: resolved
Blocked by: 02, 03

## Objective
Support `--profile core` and `--profile aggressive` in `src/main.py` with full sandbox isolation (separate SQLite databases `data/trading.db` vs `data/trading_aggressive.db`, isolated leases and circuit breakers).

## Comments
Added profile and universe selectors to `TradingDeskOrchestrator` and CLI parser in `src/main.py`. Aggressive profile automatically defaults to `data/trading_aggressive.db` and the aggressive universe with Strategy Gamma enabled.

## Answer
Implemented dual desk orchestrator support with isolated database paths, custom screener configurations, and profile-aware execution logging.

