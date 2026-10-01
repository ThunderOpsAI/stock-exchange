# Wayfinding Map: Stock Exchange Autonomous Trading Bot

## Destination

A comprehensive architecture and technical specification for an autonomous trading bot (modular broker adapter, deterministic risk engine, hybrid quant + LLM committee strategy, two-tier backtesting pipeline, SQLite audit persistence, and Streamlit/Telegram observability) operating on a $100 initial capital sandbox, ready for build execution.

## Notes

- Domain: Quantitative finance, algorithmic trading, broker automation, LLM multi-agent reasoning, deterministic risk management.
- Capital constraints: $100 initial capital sandbox; fractional shares required; max risk $20-$25 per position; hard circuit breaker stop at $80 portfolio value.
- Style: Deep modular interfaces, clear seams, decoupled execution from reasoning.
- Issue tracker: Local markdown under `.scratch/stock-exchange-bot/issues/`.

## Decisions so far

<!-- the index — one line per closed ticket: enough to judge relevance, then zoom the link for the detail the ticket holds -->

- [[01] eToro Automation & Broker Adapter Interface Specification](file:///Users/Thunderops/Documents/Projects/stock-exchange/.scratch/stock-exchange-bot/issues/01-etoro-broker-adapter.md) — Rejected browser automation; targeted official Public Developer API (`public-api.etoro.com`) with `AbstractBrokerAdapter` protocol supporting simulated paper, Alpaca, and eToro. (Captured on branch `research/01-etoro-broker-adapter`)
- [[02] Quantitative Swing Screening Strategies for $100 Capital](file:///Users/Thunderops/Documents/Projects/stock-exchange/.scratch/stock-exchange-bot/issues/02-quantitative-swing-screening.md) — Dual trend-pullback (60%) and oversold mean-reversion (40%) setups on S&P/Nasdaq leaders with 3 slots ($28-$30) and 10% cash buffer. (Captured on branch `research/02-quantitative-swing-screening`)
- [[03] Multi-Agent LLM Committee Deliberation Protocol](file:///Users/Thunderops/Documents/Projects/stock-exchange/.scratch/stock-exchange-bot/issues/03-llm-committee-deliberation.md) — Triad committee with 49% Risk Officer / 25.5% Sentiment / 25.5% Structure weights; deadlock triggers HITL Telegram escalation report with interactive action buttons; compact JSON digest input.
- [[04] Deterministic Risk Engine & Hard Circuit Breaker Specification](file:///Users/Thunderops/Documents/Projects/stock-exchange/.scratch/stock-exchange-bot/issues/04-deterministic-risk-engine.md) — Two-tier circuit breaker (soft buy halt at $80; hard liquidation floor at $70); 3 slots ($28-$30) with 10% cash buffer; max $3.00 risk cap per trade; dual-layer bracket watchdog.
- [[05] Two-Tier Backtesting Engine & Historical Replay Architecture](file:///Users/Thunderops/Documents/Projects/stock-exchange/.scratch/stock-exchange-bot/issues/05-two-tier-backtesting-engine.md) — Tier 1 macro vectorized matrix (5-10 yrs) and Tier 2 event-driven stress replay with SQLite WAL SHA-256 LLM deliberation cache and overnight gap slippage. (Captured on branch `research/05-two-tier-backtesting-engine`)
- [[06] Persistence Schema & Domain Event Store](file:///Users/Thunderops/Documents/Projects/stock-exchange/.scratch/stock-exchange-bot/issues/06-persistence-event-schema.md) — Verified SQLite WAL relational schema spanning market snapshots, candidates, LLM deliberations, HITL verdicts, orders, fills, and portfolio equity snapshots. (Prototype in `prototypes/storage_schema/`)
- [[07] Observability Dashboard & Telegram Alert/Halt Command Wireframe](file:///Users/Thunderops/Documents/Projects/stock-exchange/.scratch/stock-exchange-bot/issues/07-observability-streamlit-telegram.md) — Working Streamlit dashboard prototype with metric cards, position tables, equity charts, and LLM thought logs; Telegram bot protocol with interactive deadlock action buttons and remote kill switches. (Prototype in `prototypes/observability/`)

## Not yet specified

<!-- Fog of war: in-scope decisions not yet sharp enough to ticket until frontier tickets resolve -->

- **Production Cloud Host & Secret Vaulting**: VM hosting (AWS EC2 / Hetzner / local Raspberry Pi), environment isolation, automated process restart (systemd/Docker), and encrypted credential storage for broker passwords and API tokens.
- **Forward Paper Qualification Criteria**: Objective numerical criteria (Sharpe ratio, max drawdown, win rate over minimum 20 trades) that must be passed in paper-trading before enabling live execution with the $100 capital.
- **Automated Tax & PnL Accounting**: Long/short gain calculations, wash sale tracking, and tax export format.
- **Post-$100 Capital Scaling**: Rules for capital injection, position scaling, and transitioning to professional institutional prime brokers if the sandbox succeeds.

## Out of scope

<!-- Work consciously ruled beyond the destination -->

- High-frequency trading (HFT) and sub-second order book arbitrage: Ruled out due to retail execution latency, LLM inference latency, and fee drag on $100 capital.
- Options and leveraged margin/CFD trading: Ruled out to prevent liquidation risks and negative balances.
- Training proprietary LLM weights from scratch: Ruled out; system will leverage existing frontier reasoning models via API.
