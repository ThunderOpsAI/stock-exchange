# Observability Dashboard & Telegram Alert/Halt Command Wireframe

Type: prototype
Status: resolved
Blocked by: 06 (resolved)

## Question

What interactive Streamlit dashboard layout and Telegram bot command/alert protocol (including status queries, trade execution cards with LLM rationale, and an emergency remote HALT/LIQUIDATE kill-switch command) should be specified for real-time monitoring of the $100 portfolio?

## Answer

Prototyped and specified complete dual-surface observability:
1. **Interactive Streamlit Dashboard ([prototypes/observability/app.py](file:///Users/Thunderops/Documents/Projects/stock-exchange/prototypes/observability/app.py))**:
   - Top metrics header: Total Equity, Cash Reserve ($10+ buffer status), Active Slots (1/3), Circuit Breaker Tier (Normal / Soft / Emergency), and $3.00 Max Risk Cap.
   - Live portfolio equity curve line chart vs SPY benchmark.
   - Active swing positions table with real-time unrealized PnL, stop-loss and take-profit distances, and holding day counter.
   - Collapsible LLM Committee Thought Journal displaying full rationale, scores, and catalysts/risks for Sentiment (25.5%), Technical (25.5%), and Adversarial Risk Officer (49.0%).
   - Operator emergency control panel with soft freeze and emergency liquidation triggers.
2. **Telegram Bot & HITL Escalation Protocol ([prototypes/observability/telegram_bot_spec.md](file:///Users/Thunderops/Documents/Projects/stock-exchange/prototypes/observability/telegram_bot_spec.md))**:
   - Management commands: `/status`, `/positions`, `/journal [ticker]`, `/soft_freeze`, `/emergency_liquidate`, `/resume`.
   - Single unified interactive deadlock escalation card with inline action buttons (`[Approve Trade ($28.50)]`, `[Reject Trade]`, `[Inspect Full Trace]`) with a 15-minute fail-safe timeout.

## Comments
