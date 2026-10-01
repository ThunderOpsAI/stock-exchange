# Deterministic Risk Engine & Hard Circuit Breaker Specification

Type: grilling
Status: resolved
Blocked by: 01, 02 (resolved)

## Question

What are the exact order state machine transitions, fractional position sizing formulas for a $100 capital base (e.g., max 3-4 concurrent positions of $20-$25), bracket stop-loss/take-profit calculation rules (ATR vs percentage), and unconditional kill switch triggers (automatic total portfolio liquidation and trade halt if balance <= $80)?

## Answer

1. **Two-Tier Circuit Breaker Architecture**:
   - **Tier 1 Soft Freeze (Equity <= $80.00)**: Instantly halts all new buy entries. Open positions are permitted to run their existing ATR stop-loss and take-profit brackets to conclusion without panic selling. Dispatches a priority Telegram warning.
   - **Tier 2 Emergency Floor (Equity <= $70.00)**: Emergency total market liquidation of all remaining active positions. Creates a persistent `HALTED.lock` file preventing process auto-restart until manual operator reset via Telegram or CLI.
2. **Fractional Position Sizing & Micro-Risk Sizing ($100 Base)**:
   - **Slot Capacity**: Max 3 concurrent positions ($28–$30 allocation each).
   - **Cash Reserve**: Enforces a strict $10.00 (10%) uninvested cash buffer to absorb spread drag and margin swings.
   - **Hard Risk Cap ($R_{cap}$)**: Max total risk per position is capped at **$3.00 (3% of $100 portfolio)**. If raw slot sizing yields $Qty \times (Entry - SL) > \$3.00$, allocated capital is scaled down: $\text{Capital} = \min(\$30.00, \frac{\$3.00 \times Entry}{Entry - SL})$.
   - **Precision**: Fractional shares floored to 4 decimals; residual cents returned to cash reserve.
3. **Bracket Stop-Loss & Take-Profit Rules**:
   - *Trend Pullback*: $SL = \min(Low_{t-1}, Low_t) - 0.5 \times ATR_{14}$; $TP = Entry + 2.0R$; Max 10-day time stop.
   - *RSI Oversold*: $SL = Low_t - 1.0 \times ATR_{14}$; $TP = EMA_{10}$ or $1.5R$; Max 4-day time stop.
   - Dual-layer bracket enforcement: Order submitted with native broker bracket parameters on eToro, with local Risk Engine watchdog monitoring every bar close for gap-through slippage or broker bracket detachment.
4. **Order State Machine Transitions**:
   `PENDING_RISK_CHECK` $\to$ `RISK_APPROVED` (or `RISK_REJECTED`) $\to$ `ROUTED_TO_BROKER` $\to$ `FILLED` $\to$ `POSITION_ACTIVE` $\to$ `BRACKET_EXIT_PENDING` $\to$ `POSITION_CLOSED`.

## Comments
