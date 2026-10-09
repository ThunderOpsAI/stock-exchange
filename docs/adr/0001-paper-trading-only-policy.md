# ADR 0001: Strict Paper-Trading-Only Policy and Execution Guardrails

## Status
Accepted

## Context
The autonomous stock exchange trading desk operates algorithmic components—spanning market data ingestion, quantitative screening, LLM-assisted deliberation, deterministic risk management, and broker execution adapters. Operating algorithmic trading systems with live market capital entails substantial financial, regulatory, operational, and counterparty risks. 

In early and developmental stages of the system, execution invariants, order reconciliation loops, network retry semantics, and failure handling are actively evolving. The project is governed by strict capital constraints and domain invariants:
- Exactly **$100.00 USD** initial sandbox capital.
- Maximum of **3 concurrent positions** allocated between $28.00 and $30.00 each.
- Permanent uninvested cash buffer of **$10.00 (10%)**.
- Hard risk cap per trade ($R$) of **$3.00 (3.0% of $100)**.
- Two-tier circuit breaker: Soft freeze at $\le \$80.00$ equity, and hard liquidation floor at $\le \$70.00$ equity.

Prior prototype code contained preliminary broker adapter interfaces (such as Alpaca and eToro). Without strict architectural boundaries, misconfiguration, credential leakage, or flawed automation could inadvertently trigger live orders or bind real financial capital.

## Decision
1. **Mandatory Paper-Trading-Only Policy**:
   - The system is strictly restricted to paper-trading, simulated execution, and sandbox environments across all runtime environments, CLI modes, and test suites.
   - Live account execution, real capital commitment, and submission of real-money orders are strictly prohibited.
2. **Broker Adapter Constraints**:
   - Supported execution adapters are limited to local simulation (`SimulatedPaperBroker`) and explicitly designated paper/demo sandbox APIs (e.g., Alpaca Paper API, eToro Demo Sandbox).
   - Under no circumstances shall broker adapters target production, live exchange, or real account endpoints.
   - Scraping or headless browser automation (Playwright/Selenium) against broker web interfaces is strictly prohibited due to anti-bot mechanisms and terms-of-service violation risks.
3. **Execution Safety Gates**:
   - The runtime configuration must default to paper/dry-run mode.
   - Any attempt to configure, supply, or inject live trading credentials or production endpoints must cause the system to fail immediately at startup.
   - Execution gateways must validate broker account types, confirming that connected accounts are explicitly marked as "paper", "sandbox", or "demo".
4. **Governance for Live Trading Capabilities**:
   - Enabling any live-trading capability, live broker connectivity, or real-money execution is completely out of scope and requires a separate, human-authored and human-approved Architectural Decision Record (ADR).
   - No autonomous agent, subagent, automated script, or runtime flag possesses the authority to bypass this restriction or transition the system to live trading.

## Alternatives Considered
- **Dual-Mode Toggle via Environment Variables**: Providing a runtime switch (e.g., `EXECUTION_MODE=live|paper`) was considered. This was rejected due to the catastrophic risk of accidental live enablement through environment misconfiguration, automated CI leaks, or operator error during early development phases.
- **Micro-Capital Live Proving ($100 Live)**: Deploying the system directly to a live $100 broker account was considered. This was rejected because the operational safety stack—reconciliation, fail-closed data gates, broker-native bracket verification, and durable run leases—must first be proven robust over extended paper-trading runs before live capital is ever evaluated.

## Consequences
### Positive
- Guarantees zero risk of accidental capital loss, unauthorized trade placement, or broker financial liability.
- Enables safe local development, CI testing, and failure-injection experiments without live financial exposure.
- Enforces clear separation between strategy evaluation and actual capital risk.

### Negative
- Paper trading may experience idealized fills, lack real-market liquidity constraints, or underestimate live execution slippage.
- Cannot observe live broker order book interaction or exchange-specific microstructure delays until a formal live trading review is conducted under a future human ADR.

## Migration / Implementation Impact
- All broker adapters (`src/broker/`) must implement explicit guards asserting that endpoints and account modes are paper/demo sandboxes.
- Configuration schemas must reject live credentials or abort initialization if non-sandbox endpoints are provided.
- Documentation, test suites, and operational runbooks must maintain consistent language designating the system as paper-trading only.
