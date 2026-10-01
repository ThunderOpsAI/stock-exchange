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
