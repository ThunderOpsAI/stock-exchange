# eToro Automation & Broker Adapter Interface Specification

Type: research
Status: resolved
Blocked by: none

## Question

How can the system reliably automate orders on eToro given its lack of a public retail API (evaluating Playwright/browser automation, session token reverse engineering, 2FA/TOTP handling, headless execution stability, rate limits, and risk of account flagging), and how should the Broker Adapter Interface be defined to support paper trading (e.g. simulated exchange / Alpaca) alongside eToro?

## Answer

1. **Integration Method**: Rejected browser automation (Playwright/Selenium) due to anti-bot measures (Akamai/Cloudflare/CDP detection) and strict Terms of Service bans. Identified eToro's official **Public Developer API** (`https://public-api.etoro.com`) with API/User key authentication supporting Demo and Real modes ($10 minimum order size).
2. **Interface Architecture**: Designed `AbstractBrokerAdapter` protocol with uniform domain models (`OrderRequest`, `Position`, `AccountBalance`, `OrderResult`) and factory-based dependency injection.
3. **Phased Rollout**:
   - `SimulatedPaperBroker`: In-memory/SQLite engine for deterministic testing and backtesting.
   - `AlpacaPaperBroker`: Zero-cost live market paper testing.
   - `EtoroBrokerAdapter`: Official REST API on eToro Demo sandbox first, transitioning to Real with the $100 capital once forward tests pass.

Full research report captured on branch `research/01-etoro-broker-adapter` in `docs/research/01-etoro-broker-adapter.md`.

## Comments
