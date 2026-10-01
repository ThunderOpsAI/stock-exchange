# Telegram Bot & HITL Escalation Protocol Specification

## 1. Bot Architecture & Capabilities
The Telegram bot acts as the mobile cockpit and real-time incident controller for the $100 trading desk.

### Core Bot Commands
- `/status` — Fetches current portfolio balance, cash reserve, active slot count (e.g. 1/3), today's PnL, and circuit breaker status.
- `/positions` — Returns active holdings with unrealized PnL, current price, entry price, stop-loss distance, and holding days.
- `/journal [ticker]` — Retrieves the latest LLM committee deliberation reasoning trace and scores for a specific ticker or the last trade.
- `/soft_freeze` — Operator-initiated soft halt (freezes new buy entries; lets active stops run).
- `/emergency_liquidate` — Operator emergency kill switch (immediately executes market liquidation of all open positions and creates `HALTED.lock`).
- `/resume` — Removes `HALTED.lock` and reactivates automated trading.

---

## 2. Interactive HITL Escalation Card (When 51% vs 49% Deadlock Occurs)

When the Sentiment Analyst (25.5%) and Technical Analyst (25.5%) vote BULLISH (51% combined) but the Adversarial Risk Officer (49.0%) votes VETO, the bot dispatches an immediate interactive message with inline action buttons:

```
[TRADE DELIBERATION SPLIT: NVDA]
Strategy: TREND_PULLBACK (20 EMA)
Target Sizing: $28.50 (~0.22 shares @ $128.50)
Bracket: SL $124.20 (-$0.94 / -3.3%) | TP $137.10 (+$1.89 / +6.7%)
Risk / Reward: 2.0R ($0.94 risk vs $1.89 reward)

--- ANALYST CASES ---
🟢 Sentiment (25.5%): 8.5/10 - Strong data center momentum; 0 binary earnings risk for 38 days.
🟢 Technical (25.5%): 8.0/10 - Textbook 20 EMA pullback bounce with 1.35x RVOL reversal.
🔴 Risk Officer (49.0%): 3.5/10 [VETO] - FOMC rate announcement in 48 hours introduces macro gap risk.

Status: HITL DEADLOCK (51% Bullish vs 49% Risk Dissent)
Awaiting Human Operator Decision:

[Approve Trade ($28.50)]  [Reject Trade]  [Inspect Full Trace]
```

### Callback Button Handler Logic:
1. `Approve Trade`: Callback executes `OrderRequest` routing via `AbstractBrokerAdapter`, transitions candidate to `HUMAN_APPROVED`, and updates message to show `APPROVED by Operator at 09:42 EST`.
2. `Reject Trade`: Cancels order routing, transitions candidate to `HUMAN_REJECTED`, and confirms `REJECTED by Operator`.
3. `Timeout (15 minutes)`: If no operator response occurs before 15:55 EST (market close approach), candidate transitions to `TIMED_OUT` and aborts trade safely.
