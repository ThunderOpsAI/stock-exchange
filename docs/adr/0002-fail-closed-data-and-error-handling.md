# ADR 0002: Mandatory Fail-Closed Semantics for Market Data and Error Handling

## Status
Accepted

## Context
Automated algorithmic trading requires timely, consistent, and authentic market signals. Trading on stale, missing, truncated, or contradictory data creates severe vulnerabilities, such as executing on ghost quotes, miscalculating risk-adjusted position sizes, failing to honor stop-loss thresholds, or entering positions immediately prior to unannounced corporate actions or during market halts.

In the initial system prototype:
- Market data fetches frequently utilized blanket `except Exception:` handlers with fallback heuristics or stale cache lookups up to 12 hours old.
- Spread estimations defaulted to static values (e.g., hard-coded 3 basis points) when live bid/ask quotes were absent.
- Macro screening logic silently permitted execution even when benchmark data (e.g., SPY daily series) was incomplete or unavailable.
- Transient provider timeouts logged warnings but allowed subsequent deliberation and execution pipelines to continue.

Such "fail-open" or best-effort fallbacks violate safety principles. A trading desk must favor missing an opportunity over entering an unhedged or unverified position based on degraded inputs.

## Decision
1. **Mandatory Fail-Closed Semantics**:
   - The system strictly adheres to fail-closed error handling across all market data ingestion, screening, risk assessment, and execution workflows.
   - If required data is missing, stale, malformed, or contradictory, the system must immediately block trade entries and return a deterministic `NO_TRADE` outcome.
   - Under no circumstances is a "warning plus a trade" permitted.
2. **Explicit Data Provenance and Freshness**:
   - All market data payloads (quotes, historical bars, news sentiment, corporate actions) must be typed and accompanied by explicit acquisition timestamps, provider metadata, and freshness indicators.
   - Real-time execution decisions must rely on discrete bid and ask quotes with timestamps within an established freshness window. Estimated or synthetic spreads are strictly forbidden for order sizing or execution entry.
3. **Session, Calendar, and Corporate Action Gates**:
   - New orders are strictly blocked if:
     - Market session is closed, unknown, or outside regular trading hours.
     - The target asset is halted or under an active regulatory trading restriction.
     - An upcoming corporate action (stock split, reverse split, dividend ex-date, earnings release within the blackout threshold) introduces binary pricing risk.
4. **Authoritative Deterministic Risk & Circuit Breakers**:
   - The deterministic `RiskEngine` is authoritative over all system components. Neither LLM consensus, operator recommendations, nor screener scores may override or bypass risk gates.
   - Breaching any invariant ($100 sandbox capital cap, $3 max loss per trade, 3 open position limit, $10 permanent cash buffer) results in immediate entry rejection.
   - A Tier 1 soft freeze ($\text{Equity} \le \$80.00$) halts opening new positions. Soft freeze state is persisted to disk and survives process restarts.
   - A Tier 2 hard liquidation floor ($\text{Equity} \le \$70.00$) triggers emergency market liquidation and persists a `HALTED.lock` requiring manual intervention.
5. **Ambiguous and Degraded Execution Handling**:
   - Any unhandled exception, network timeout during broker order submission, or ambiguous order status halts further entries and places the transaction into an `UNKNOWN_PENDING_RECONCILIATION` state.
   - No blind retries are executed; state must be explicitly resolved via post-submission reconciliation.

## Alternatives Considered
- **Fail-Open with Best-Effort Fallbacks**: Allowing the screener and risk engine to use stale cached bars or default spreads during provider outages. This was rejected because executing on obsolete prices frequently causes adverse selection, excessive slippage, and stop-loss breaches.
- **Degraded Sizing Mode**: Dynamically reducing position sizes (e.g., 50% allocation) when data feeds are degraded. This was rejected because partial data degradation still leaves the system blind to critical volatility and structural risks.

## Consequences
### Positive
- Prevents erroneous or out-of-bounds trades caused by stale data, network dropouts, or malformed provider responses.
- Produces deterministic, auditable failure states with structured reasons recorded in the audit log.
- Establishes a predictable baseline where every executed order is verified against fresh, authoritative market data.

### Negative
- Higher rate of false-positive rejections (missed trade entries) during intermittent network jitter or provider latency spikes.
- Requires strict health monitoring and structured error models across all data providers and pipelines.

## Migration / Implementation Impact
- Refactor `src/data/pipeline.py` to eliminate blanket exception suppression and return typed health and freshness results (`DataHealthResult`).
- Update screener and macro filter logic to enforce explicit data availability gates before generating candidate signals.
- Update `RiskEngine` and `ExecutionGateway` to require verified quote freshness, persistent soft-freeze checks, and atomic reconciliation on timeouts.
