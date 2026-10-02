# Research Report: Ticket 05 — Two-Tier Backtesting Engine & Historical Replay Architecture

## 1. Executive Summary & Design Rationale

Operating an autonomous quantitative swing trading system with a **$100 capital base** and hybrid **LLM Committee Deliberation** poses two conflicting backtesting requirements:
1. **Macro Edge Validation (Speed & Breadth)**: Evaluating whether the dual quantitative screening strategies (Trend-Leader 20 EMA Pullback and 14-day RSI Oversold Mean Reversion) have positive mathematical expectancy ($E > 0$), acceptable Sharpe/Sortino ratios, and tolerable drawdown across **5 to 10 years** across 100+ S&P 500 / Nasdaq 100 constituents.
2. **Microstructure & Regime Stress Fidelity (Depth & Determinism)**: Testing how the system executes during historical market crises (2020 COVID crash, 2022 rate hike drawdown, 2023 SVB banking shock, Aug 2024 VIX spike) with fractional share rounding, bid-ask spread crossing, overnight gap slippage, slot contention (3 slots of $28-$30 + $10 buffer), and hard circuit breaker triggers ($Balance \le \$80$).

### Two-Tier Decoupled Architecture
- **Tier 1 (Macro Vectorized Backtesting)**: High-speed matrix simulation across 5-10 years of daily OHLCV bars. Validates raw quant signal expectancy, win rates, streak statistics, and parameter sensitivity in seconds.
- **Tier 2 (Event-Driven Historical Replay & LLM Cache)**: Full-fidelity, bar-by-bar state machine replay across curated volatility stress regimes. Integrates an **SQLite Write-Ahead-Logging (WAL) LLM Deliberation Cache** with SHA-256 content-addressable keying, realistic fractional execution, dynamic spread widening, overnight stop-loss gap slippage, and latency drift.

---

## 2. Tier 1: Macro Vectorized Backtesting Design

- **Lookahead Bias Prevention**: Indicators at bar $t$ use data up to $Close_t$. Entry signals at bar $t$ explicitly shift by 1 bar ($\text{Entry}[t] = \text{Signal}[t-1]$). Execution fills at $Open_t$ plus half-spread slippage.
- **Slot Contention Matrix**: At bar $t-1$, candidate signals are ranked using the Ticket 02 composite Z-score. A fast Numba loop allocates capital only to the top $K = (3 - \text{Occupied Slots})$ candidates; lower-ranked signals are marked `SKIPPED_CAPACITY`.
- **Target Acceptance Metrics ($100 Sandbox)**:
  - Mathematical Expectancy $E \ge 0.50R$ per trade
  - Profit Factor $PF \ge 1.60$
  - Sharpe Ratio $\ge 1.20$, Sortino Ratio $\ge 1.70$
  - Maximum Drawdown $\le 15.0\%$ ($15 on $100)
  - Circuit Breaker Breach ($Balance \le \$80$): **0 breaches allowed across 10 years**.

---

## 3. Tier 2: Event-Driven Historical Replay & Stress Regimes

- **Curated Stress Regimes**:
  1. *2020 COVID Crash (Feb 18 – June 1, 2020)*: 34% drop, VIX 82.7, overnight gap-downs, spread blowout.
  2. *2022 Fed Rate-Hike Drawdown (Jan 3 – Oct 31, 2022)*: Prolonged tech bear market, failed rallies.
  3. *2023 SVB Regional Banking Shock (March 6 – April 28, 2023)*: Sector contagion and divergence.
  4. *August 2024 VIX Spike & Yen Carry Unwind (Aug 1 – Aug 16, 2024)*: Gap-down and intraday hammer recovery.

---

## 4. Deterministic LLM Deliberation Caching

- **Content-Addressable SHA-256 Key**:
  $$\text{CacheKey} = \text{SHA256}(\text{symbol} + \text{as\_of\_date} + \text{strategy\_id} + \text{quant\_features} + \text{point\_in\_time\_news\_hash} + \text{agent\_role} + \text{prompt\_hash} + \text{model\_name})$$
- **SQLite WAL Schema**:
  High-concurrency SQLite table `deliberation_cache` in WAL mode with indexes on `(symbol, as_of_date, agent_role)`.
- **Execution Modes**:
  - `REPLAY_STRICT`: Offline, 100% deterministic, 0 cost (requires cache hit).
  - `RECORD_ON_MISS`: Hits cache in <0.5ms; on miss, calls API and stores response.
  - `SYNTHETIC_MOCK`: Fast heuristic oracle for CI/CD test passes.

---

## 5. Microstructure & Sizing Modeling for $100 Capital

- **Fractional Share Precision**: Floor to 4 decimal places; residual cents stay in cash buffer.
- **Dynamic Volatility Spread Multiplier**:
  $$\text{Spread}_{\text{eff}}(t) = \text{Spread}_{\text{base}} \times \max\left(1.0, \; \frac{ATR_{14}(t)}{\overline{ATR}_{63}(t)}\right)$$
- **Overnight Gap-Through Stop Loss**: If $Open_t < SL$, fill at $Open_t$ minus spread penalty (never at nominal stop price).
- **Latency Drift**: Gaussian random walk modeled price drift during opening execution window (09:35–09:40 EST).
