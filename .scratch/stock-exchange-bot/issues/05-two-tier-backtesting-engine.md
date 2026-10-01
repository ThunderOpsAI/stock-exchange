# Two-Tier Backtesting Engine & Historical Replay Architecture

Type: research
Status: resolved
Blocked by: 02 (resolved)

## Question

What is the optimal architectural design and event loop for a Two-Tier backtesting harness (Tier 1: high-speed vectorized backtesting over 5-10 years of daily price data to validate quant signal edges; Tier 2: event-driven historical replay of volatility stress periods with realistic slippage modeling, fee structures, and LLM deliberation response caching)?

## Answer

1. **Two-Tier Engine**:
   - **Tier 1 (Macro Vectorized)**: VectorBT/Numba array backtester across 5–10 years daily OHLCV; strict anti-lookahead shifting ($\text{Entry}[t] = \text{Signal}[t-1]$, $Open_t$ fills); slot contention limiter enforcing max 3 positions on $100 capital. Target thresholds: $E \ge 0.50R$, $PF \ge 1.60$, Sharpe $\ge 1.20$, $MDD \le 15\%$, 0 circuit breaker breaches.
   - **Tier 2 (Event-Driven Replay)**: Bar-by-bar state machine tested on 4 historical stress regimes (2020 COVID, 2022 Rate Hike, 2023 SVB, Aug 2024 VIX spike). Models fractional 4-decimal precision, dynamic spread widening, overnight stop gap-throughs, and execution latency drift.
2. **Deterministic LLM Caching**:
   - Content-addressable SHA-256 keying of inputs, point-in-time news hashes, and prompt versions into an SQLite WAL cache (`data/cache/llm_deliberations.db`).
   - Supports `REPLAY_STRICT` (100% deterministic, $0 API cost) and `RECORD_ON_MISS`.

Full research report captured on branch `research/05-two-tier-backtesting-engine` in `docs/research/05-two-tier-backtesting-engine.md`.

## Comments
