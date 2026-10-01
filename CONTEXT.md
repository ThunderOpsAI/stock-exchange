# Context & System Architecture: Autonomous Stock Exchange Trading System

## 1. Domain Invariants & Capital Constraints
- **Initial Capital Sandbox**: Exactly **$100.00 USD**.
- **Portfolio Slot Sizing**: Maximum of **3 concurrent positions** at **$28.00–$30.00** each.
- **Uninvested Cash Buffer**: Permanent reserve of **$10.00 (10%)** to absorb bid-ask spread drag, broker markups, and margin variance.
- **Hard Risk Cap per Trade ($R$)**: Maximum risk per trade is strictly capped at **$3.00 (3.0% of $100)**:
  $$\text{Capital Allocated} = \min\left(\$30.00, \; \frac{\$3.00 \times \text{Entry}}{\text{Entry} - \text{StopLoss}}\right)$$
- **Two-Tier Circuit Breaker**:
  - *Tier 1 Soft Freeze ($\text{Equity} \le \$80.00$)*: Halts opening any new buy positions; active positions run their existing ATR stop/target brackets.
  - *Tier 2 Hard Liquidation Floor ($\text{Equity} \le \$70.00$)*: Executes emergency market liquidation of all remaining active positions; creates a persistent `HALTED.lock` file requiring manual operator reset.
- **Microstructure & Asset Universe**:
  - Direct unleveraged US Equities (S&P 500 & Nasdaq 100 leaders) and liquid Sector ETFs (SPY, QQQ, XLK, XLE, etc.).
  - Filters: ADDV20 $\ge \$25\text{M}$, relative spread $\le 0.06\%$ (6 bps), price $\ge \$15.00$.
  - Minimum trade amount: $10.00 (eToro minimum threshold).
  - Fractional share precision: 4 decimal places.

---

## 2. System Topology & Seams

```
+-------------------------------------------------------------------------------------------------+
|                                    AUTONOMOUS TRADING DESK                                      |
+-------------------------------------------------------------------------------------------------+
                                                  |
           +--------------------------------------+--------------------------------------+
           |                                                                             |
           v                                                                             v
+-------------------------------+                                         +-------------------------------+
|  Tiered Market Data Pipeline  |                                         |   Streamlit & Telegram Desk   |
| (yfinance, Finnhub, SEC EDGAR)|                                         | (Live Equity, LLM Log, HITL)  |
+-------------------------------+                                         +-------------------------------+
           |                                                                             ^
           v                                                                             |
+-------------------------------+         +-------------------------------+              |
|  Quantitative Swing Screener  | ------> |      LLM Deliberation Desk    | -------------+
| (Trend Pullback & RSI Bounce) |         | (Sentiment, Tech, Risk Veto)  |
+-------------------------------+         +-------------------------------+
                                                          |
                                                          v (Approved / HITL Consensus)
                                          +-------------------------------+
                                          |   Deterministic Risk Engine   |
                                          | (Two-Tier Kill Switch, Slots) |
                                          +-------------------------------+
                                                          |
                                                          v (OrderRequest)
                                          +-------------------------------+
                                          |     AbstractBrokerAdapter     |
                                          | (Simulated / Alpaca / eToro)  |
                                          +-------------------------------+
                                                          |
                                                          v
                                          +-------------------------------+
                                          |   SQLite WAL Domain Storage   |
                                          |  (Market, Orders, Audit Logs) |
                                          +-------------------------------+
```

---

## 3. Deliberation Committee & Consensus Matrix

| Agent Persona | Voting Weight | Primary Responsibility | Focus / Analysis Scope |
| :--- | :--- | :--- | :--- |
| **Sentiment & Catalyst Analyst** | **25.5%** | Bullish catalyst validation | SEC filings, news sentiment (last 72h), analyst upgrades, earnings calendar distance ($>7$ days). |
| **Technical Structure Analyst** | **25.5%** | Price geometry & volume audit | 20 EMA pullback quality, volume contraction on dip, 1.20x+ RVOL reversal, relative strength ($RS_{SPY} \ge 1.05$). |
| **Adversarial Risk Officer** | **49.0%** | Downside stress testing & veto | Binary macro risk (FOMC, CPI), sector contagion, trapped volume, adverse liquidity. |

### Consensus State Machine:
1. **Auto-Approval ($\text{Score} \ge 74.5\%$)**: Risk Officer approves + at least one analyst approves $\implies$ routes immediately to Risk Engine.
2. **Deadlock / Split Decision (51% Analysts vs 49% Risk Dissent)**: Auto-execution is blocked. Dispatches an interactive **HITL Telegram Escalation Brief** with `[Approve Trade ($28.50)]` and `[Reject Trade]` inline buttons (15-minute fail-safe timeout).
3. **Auto-Drop**: Both analysts reject $\implies$ candidate dropped with zero operator interruption.

---

## 4. Broker Adapter Interface
- **Target Integration**: Official **eToro Public Developer API** (`https://public-api.etoro.com`) via `x-api-key`, `x-user-key`, and idempotent UUIDv4 `x-request-id`.
- **Prohibited**: Headless browser automation (Playwright/Selenium) is banned due to Akamai Bot Manager defenses and strict Terms of Service violation risks.
- **Adapter Tiers**:
  - `SimulatedPaperBroker`: Local deterministic in-memory/SQLite testing and backtesting.
  - `AlpacaPaperBroker`: Forward live market testing against real tick streams.
  - `EtoroBrokerAdapter`: Production deployment on eToro Demo sandbox first, transitioning to Real with the $100 capital.

---

## 5. Verification & Backtesting Harness
- **Tier 1 (Macro Vectorized)**: Fast matrix backtester across 5–10 years of daily bars with strict anti-lookahead shifting ($\text{Entry}[t] = \text{Signal}[t-1]$, fills at $Open_t$). Targets: $E \ge 0.50R$, Sharpe $\ge 1.20$, $MDD \le 15\%$, 0 circuit breaker breaches.
- **Tier 2 (Event-Driven Replay)**: Bar-by-bar state machine across 4 crisis regimes (2020 COVID, 2022 Rate Hike, 2023 SVB, Aug 2024 VIX spike) with fractional rounding, overnight stop-loss gap slippage, and SQLite WAL SHA-256 LLM response caching.
