# Multi-Agent LLM Committee Deliberation Protocol

Type: grilling
Status: resolved
Blocked by: 02 (resolved)

## Question

What is the exact persona design, system prompt contracts, input data schema (news headlines, analyst ratings, technical summaries), scoring rubric, and consensus/veto mechanism for the LLM Committee (e.g., Sentiment Analyst, Macro/Fundamental Analyst, Contrarian/Risk Officer), and how will deterministic JSON outputs and API token expenditure be bounded?

## Answer

1. **Triad Committee Personas & Voting Weights**:
   - **Sentiment & Catalyst Analyst (25.5% weight)**: Audits news headlines, sentiment, SEC filings, sector momentum, and days-to-earnings.
   - **Technical Structure Analyst (25.5% weight)**: Validates pullback geometry, 20 EMA bounce quality, RSI divergence, RVOL conviction, and relative strength.
   - **Adversarial Risk Officer (49.0% weight)**: Actively attacks the trade thesis, looking for liquidity traps, false breakouts, macro contagion, and binary risks.
2. **Consensus & Telegram HITL Escalation Protocol**:
   - **Auto-Execution (Score >= 74.5%)**: If the Adversarial Risk Officer approves and at least one analyst approves, the trade automatically proceeds to order routing.
   - **Deadlock Split (51% Analysts vs 49% Risk Officer Veto)**: Automated execution is blocked. The bot triggers an immediate **Human-in-the-Loop (HITL) Telegram Escalation** delivering a single unified deliberation brief containing the bullish cases, the Risk Officer's dissent, and inline buttons (`[Approve Trade]`, `[Reject Trade]`, `[Inspect Details]`).
   - **Auto-Drop**: If both analysts reject, the candidate is discarded with zero alert spam.
3. **Input Data Schema & Token Budget**:
   - Compact Structured JSON Digest: Quant metrics from Ticket 02, top 5 headlines (last 72h), and earnings calendar proximity; bounded under 1,000 tokens per call (~$0.01/day).
4. **Structured Pydantic Output Contract**:
   - Each agent returns strict JSON: `agent_role`, `stance` (`BULLISH` / `NEUTRAL` / `BEARISH` / `VETO`), `score_10` (0.0–10.0), `bullish_catalysts` (list[str]), `risk_factors` (list[str]), and `rationale_summary` (concise 3-sentence summary).

## Comments
