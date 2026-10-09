# Production Readiness Specification: Autonomous Trading Desk

## 1. Purpose

Transform this repository from a well-tested prototype into a **paper-trading-first, operationally reliable trading system**. The work must improve execution safety, data integrity, recoverability, research validity, and operator control before any real-capital capability is considered.

This document is written for an orchestrator agent that delegates narrowly scoped tasks to subagents. It defines the target, constraints, work order, required outputs, and acceptance criteria.

## 2. Non-Negotiable Safety Rules

1. **Paper trading only.** Do not activate, add, or test real-money trading. No subagent may change a live broker setting, submit an external order, or use real credentials.
2. **Fail closed.** Missing, stale, malformed, contradictory, or unverified data must prevent new entries. A warning plus a trade is not acceptable.
3. **The deterministic risk engine is authoritative.** An LLM, dashboard, Telegram callback, strategy, or broker response must never bypass risk validation.
4. **No destructive Git commands.** Do not use `git reset --hard`, `git checkout --`, force pushes, or delete unrelated files.
5. **Preserve existing invariants** until deliberately superseded by an approved ADR:
   - $100 initial sandbox capital.
   - Maximum three concurrent positions.
   - $10 permanent cash buffer.
   - $3 maximum loss per trade.
   - $80 soft freeze and $70 hard-liquidation threshold.
6. **No silent fallbacks.** Every degraded dependency needs an explicit status, structured reason, audit record, and no-trade result.
7. **No credentials in source, tests, fixtures, logs, commits, or examples.** Use environment variables and redact secrets.
8. **One owner per file at a time.** The orchestrator must avoid concurrent edits to the same file.

## 3. Starting-State Assessment

The existing system has useful seams and tests:

| Area | Current location | Present capability | Important gap |
| --- | --- | --- | --- |
| Orchestration | `src/main.py` | Daily cycle and CLI modes | Four-hour loop, no durable job/run state or recovery |
| Market data | `src/data/pipeline.py` | Daily yfinance bars, local cache, indicators, headlines | 12-hour cache, broad exceptions, hard-coded 3 bps spread |
| Screening | `src/screener/screener.py` | Trend-pullback and mean-reversion setups | Static small universe and a macro-regime no-op branch |
| Committee | `src/llm/agents.py`, `src/llm/committee.py` | Persisted deterministic deliberation and cache | Agent implementations are heuristics, not model calls |
| Risk | `src/risk/engine.py` | Sizing, slots, hard lock, bracket watchdog | No portfolio-risk model; exits rely on process cadence |
| Brokers | `src/broker/` | Simulated, Alpaca, eToro adapters | Reconciliation and uncertain-outcome recovery are absent |
| Persistence | `src/storage/` | SQLite WAL domain tables and audit logs | No schema migration system or durable run/execution state |
| Operations | `src/observability/` | Dashboard and Telegram message formatting | Soft freeze is not persistent; no authenticated transport loop |
| Tests | `tests/` | Unit and synthetic end-to-end coverage | No deterministic operational failure/restart contract suite |

## 4. Definition of Done

The project is complete only when all conditions below hold in a clean checkout:

1. `pytest` passes without internet access using fixtures/mocks where required.
2. A paper-only runtime mode is the default and execution is blocked unless explicitly enabled for a paper broker.
3. Every proposed order passes a freshness, market-status, risk, and duplicate/idempotency validation path.
4. Every submitted paper order is durably recorded and reconciled after submission and after restart.
5. Data-provider failures, stale data, broker timeouts, partial fills, duplicate callbacks, and process restarts all produce deterministic, tested outcomes.
6. Protective exits are broker-native when the selected broker supports them; otherwise, the runtime explicitly marks the protection degraded and blocks entries unless a safe watchdog cadence is available.
7. Operator actions are persistent, audited, authenticated at the transport boundary, and cannot bypass the risk engine.
8. Backtests and paper-trading reports disclose assumptions, data provenance, slippage, fees, and out-of-sample/walk-forward performance.
9. The dashboard can explain each trade from source data through decision, approval, broker response, reconciliation, and outcome.
10. No item is labelled “production ready” or enables live capital. A separate, human-approved ADR would be required for any future live-trading discussion.

## 5. Architecture Target

```text
Scheduler / CLI
      |
      v
Durable Trading Run ---------------------> Audit Event Store
      |
      +--> Data Health Gate --> Versioned Market Snapshot --> Screener
      |                                                    |
      |                                                    v
      |                                           Decision Committee
      |                                                    |
      |                                        Candidate / HITL State
      |                                                    |
      +--> Portfolio Reconciliation --> Deterministic Risk Gate
                                                   |
                                                   v
                                      Idempotent Paper Execution Gateway
                                                   |
                                                   v
                                Broker-native protection + reconciliation
                                                   |
                                                   v
                                      Positions / Fills / Portfolio Snapshot
```

### Required boundaries

- `MarketDataProvider`: retrieves quotes, bars, news, calendars, and metadata; returns freshness and provenance.
- `ExecutionGateway`: wraps a broker adapter with idempotency, reconciliation, retry classification, and paper-only policy.
- `RiskEngine`: receives an immutable portfolio snapshot and proposed order; returns an allow/deny decision plus reasons.
- `TradingRunService`: owns run lifecycle and prevents overlapping runs.
- `OperatorControlService`: persists freeze/resume/approval decisions and validates actor identity before acting.
- `ResearchService`: runs reproducible backtests and writes versioned reports; it cannot submit orders.

## 6. Orchestrator Operating Protocol

### 6.1 Before delegating

1. Read `AGENTS.md`, `CONTEXT.md`, this file, and any applicable `docs/adr/` records.
2. Inspect `git status --short`. Existing changes belong to the user; never overwrite them.
3. Create or update `.scratch/production-readiness/map.md` and one ticket per work item following `docs/agents/issue-tracker.md`.
4. Establish the baseline with `pytest -q`. If baseline fails, record failures before changing code.
5. Start with Phase 0 and do not overlap dependent tickets.

### 6.2 Standard subagent brief

Every subagent prompt must include this exact contract, followed by the ticket-specific scope:

```text
You are implementing one bounded ticket in the stock-exchange repository.

Read AGENTS.md, CONTEXT.md, SPEC.md, and all applicable scoped instructions first.
This project is paper-trading only. Do not use real credentials, submit external orders,
or enable live trading. Preserve the $100 sandbox risk invariants.

Scope: <ticket scope>
Allowed files: <explicit paths>
Forbidden files: <explicit paths or “all other files”>

Requirements:
1. Make the smallest coherent change that solves the ticket.
2. Add or update focused tests for all changed behavior.
3. Run the ticket’s listed validation commands.
4. Do not modify unrelated user changes.
5. Do not commit, push, install unrelated dependencies, or make network calls.

Return only: summary, files changed, tests run/results, risks or follow-ups.
```

### 6.3 Subagent response review

After every subagent completes, the orchestrator must:

1. Inspect `git diff --check` and the relevant diff.
2. Confirm scope compliance and no credential leakage.
3. Run the ticket-level test command itself.
4. Check the contract against the ticket acceptance criteria.
5. Update the ticket status and append a short resolution to `.scratch/`.
6. Only then schedule a dependent ticket.

### 6.4 Parallelism rules

- Parallelize only independent tickets with disjoint allowed files.
- Never run two agents that edit `src/domain/models.py`, `src/storage/schema.sql`, `src/storage/db.py`, `src/main.py`, or the same test file.
- Schema/model changes must land before services that consume them.
- Risk-engine changes must land before any control-plane or execution changes that depend on their decisions.
- Run the full suite after each phase, not only at the end.

## 7. Delivery Plan

### Phase 0 — Baseline and Delivery Scaffold

**Goal:** Make the project reproducible and establish a verified baseline.

| ID | Owner | Scope | Allowed files | Acceptance criteria |
| --- | --- | --- | --- | --- |
| P0-01 | Delivery agent | Add project tooling and documented local workflow | `README.md`, `pyproject.toml`, `.github/workflows/*`, optional config files | Pinned dependencies; formatter, linter, type checker, and test commands documented; CI runs tests; no secrets |
| P0-02 | Test agent | Capture baseline and add a test marker strategy | `pytest.ini`, `tests/conftest.py`, test files | Fast/unit vs integration markers; deterministic test database and lock fixtures; existing behavior preserved |
| P0-03 | Architecture agent | Write ADRs for paper-only policy, failure policy, and schema migration approach | `docs/adr/*` | Each ADR states context, decision, alternatives, consequences, and migration impact |

**Phase gate:** `pytest -q`, lint, and type checks pass in a clean environment. No product behavior changes are required in this phase.

### Phase 1 — Safety and Data Integrity

**Goal:** Ensure a system cannot open a new position from stale, untrusted, or unavailable inputs.

| ID | Owner | Scope | Dependencies | Acceptance criteria |
| --- | --- | --- | --- | --- |
| P1-01 | Data agent | Add typed market-data result/provenance/freshness models; remove blanket exception fallback | P0-03 | Failed/stale provider returns explicit unhealthy result; no entry can use it; tests cover timeout, malformed payload, stale cache |
| P1-02 | Data agent | Separate daily historical bars from execution quote data, with bid/ask and timestamps | P1-01 | Entry pricing and spread checks use quote timestamp and bid/ask; estimated spread is never used for execution |
| P1-03 | Screening agent | Make macro filter fail closed and enforce SMA-50 deterioration rule | P1-01 | Insufficient SPY history or unavailable indicators blocks scanning; regression tests cover all branches |
| P1-04 | Runtime agent | Add market calendar, session, halt, and corporate-action entry gates | P1-01 | Closed/unknown market status, a halted symbol, or corporate-action risk blocks a new order with an audit record |
| P1-05 | Risk agent | Add a durable, persistent soft-freeze state to risk evaluation | P0-03 | Soft freeze survives restart; Telegram/dashboard controls write state; risk validation blocks entries while active |

**Phase gate:** A complete daily cycle succeeds with healthy fixtures and fails closed for each injected data/control failure. The failure reason appears in audit logs and the dashboard data source.

### Phase 2 — Durable Execution and Reconciliation

**Goal:** Eliminate duplicate/unknown execution and make broker state recoverable.

| ID | Owner | Scope | Dependencies | Acceptance criteria |
| --- | --- | --- | --- | --- |
| P2-01 | Storage agent | Introduce ordered SQLite migrations and execution/run tables | P0-03 | Migration from a fresh and existing database works; migrations are idempotent; run/order intent/reconciliation records are queryable |
| P2-02 | Domain agent | Add models and legal state transitions for run, order intent, submission, reconciliation, and degraded protection | P2-01 | Invalid transitions are rejected; all states have focused unit tests |
| P2-03 | Broker agent | Build an `ExecutionGateway` around paper brokers with idempotency and classified retry outcomes | P2-01, P2-02 | Repeated submission with same intent creates at most one broker order; timeout produces `UNKNOWN_PENDING_RECONCILIATION`, never a blind retry |
| P2-04 | Broker agent | Implement broker reconciliation on startup, before a run, and after submission | P2-03 | Missing local order, missing broker order, partial fill, changed position, and orphaned broker position are detected and audited |
| P2-05 | Risk agent | Require reconciled portfolio snapshot and reserve risk for pending/unknown orders | P2-03, P2-04 | An unreconciled account or pending unknown order blocks new entries |
| P2-06 | Runtime agent | Add singleton run lease, durable run state, retry policy, and restart recovery | P2-01, P2-04 | Concurrent run attempt is rejected; interrupted run resumes/reconciles safely; all outcomes are recorded |

**Phase gate:** Simulated tests prove no duplicate order after timeout/restart, and no new entry while reconciliation is unhealthy.

### Phase 3 — Protection and Portfolio Risk

**Goal:** Make exit protection and total exposure safer than individual-position sizing alone.

| ID | Owner | Scope | Dependencies | Acceptance criteria |
| --- | --- | --- | --- | --- |
| P3-01 | Broker agent | Describe broker protection capabilities and create/verify native bracket protection for supported paper brokers | P2-03 | Order records identify native vs watchdog protection; native leg IDs are reconciled |
| P3-02 | Risk agent | Add degraded-protection policy and watchdog cadence health checks | P3-01 | An entry is blocked if neither native protection nor an explicitly healthy watchdog is available |
| P3-03 | Risk agent | Add daily/weekly loss limits, consecutive-loss pause, and portfolio stop-risk cap | P2-05 | Each limit has boundary tests; the strictest active limit wins and writes an audit decision |
| P3-04 | Risk agent | Add sector/factor/correlation concentration caps and event-risk exclusions | P1-04, P2-05 | Correlated exposure is computed from versioned metadata; excess exposure blocks an entry |
| P3-05 | Strategy agent | Add time stops, trailing/partial exit policy, and explicit overnight/earnings rules | P3-01 | Exit policy is deterministic, persisted, and tested against gaps and missing quotes |

**Phase gate:** The system can demonstrate native protection, degraded-mode blocking, and each portfolio limit under synthetic broker scenarios.

### Phase 4 — Operator Control Plane and Observability

**Goal:** Make operational state explicit, secure, explainable, and recoverable.

| ID | Owner | Scope | Dependencies | Acceptance criteria |
| --- | --- | --- | --- | --- |
| P4-01 | Control-plane agent | Implement Telegram transport adapter, webhook/polling loop, sender authorization, and callback acknowledgement | P1-05, P2-06 | Unauthorized users cannot act; callback replay is harmless; transport errors are audited; tests use mocked HTTP only |
| P4-02 | Control-plane agent | Enforce HITL expiry and state transitions with server-side timestamps | P2-02 | Approve/reject after expiry fails; only one terminal outcome is possible; risk validation still runs on approval |
| P4-03 | Dashboard agent | Build run health, data freshness, reconciliation, protection status, and decision lineage views | P1-01, P2-04, P3-02 | Each displayed metric derives from persisted records; no reference/demo values appear as live state |
| P4-04 | Observability agent | Add structured logs, alert routing interfaces, redaction, and operational runbook | P2-06 | Critical conditions create an audit event and alert payload; secrets are redacted; runbook covers incident recovery |

**Phase gate:** A simulated operator can freeze, resume, approve, reject, and observe an expired request without bypassing risk or leaving ambiguous state.

### Phase 5 — Research Validity and Strategy Evolution

**Goal:** Ensure reported edge is credible before expanding strategy complexity.

| ID | Owner | Scope | Dependencies | Acceptance criteria |
| --- | --- | --- | --- | --- |
| P5-01 | Research agent | Create versioned data manifests with source, retrieval time, universe membership, corporate actions, and assumptions | P0-01 | Every backtest report points to a reproducible manifest and code/config version |
| P5-02 | Backtest agent | Model spread, fees, slippage, partial fills, gaps, delistings, and corporate actions | P5-01 | Tests show the costs affect results; assumptions are surfaced in reports |
| P5-03 | Research agent | Add walk-forward optimization, purged cross-validation, and held-out regime reporting | P5-02 | Parameters are never selected on evaluation data; reports distinguish train/validation/test results |
| P5-04 | Strategy agent | Introduce dynamic, liquid universe construction and version it per run | P5-01 | Eligibility records include liquidity, price, tradability, and reason for inclusion/exclusion |
| P5-05 | Analytics agent | Add paper-trading calibration and attribution reports | P2-04, P5-01 | Report reconciles predicted versus realized fill/return, slippage, risk veto effect, and strategy contribution |

**Phase gate:** A research report can be independently reproduced from its manifest and clearly reports out-of-sample performance and costs.

### Phase 6 — Decision Intelligence (Optional, After All Earlier Gates)

**Goal:** Improve decision quality without giving non-deterministic components execution authority.

| ID | Owner | Scope | Dependencies | Acceptance criteria |
| --- | --- | --- | --- | --- |
| P6-01 | LLM agent | Add an optional real provider adapter behind structured schemas, timeouts, cost limits, and cached replay | P1-01, P2-06 | Invalid response, provider failure, and cost cap result in explicit no-trade/degraded behavior; no key appears in test output |
| P6-02 | LLM agent | Enrich digest with real earnings, filings, news provenance, macro events, and portfolio context | P5-01 | Every input carries timestamp/source; unavailable required context blocks the affected decision |
| P6-03 | Analytics agent | Measure heuristic/model calibration, disagreement, override results, and veto precision | P5-05, P6-01 | Dashboard/report shows decision quality; no metric alone changes risk limits automatically |

**Phase gate:** Model capability is optional and removable; its failure cannot weaken deterministic safety controls.

## 8. Test Strategy

### Required test levels

1. **Unit tests:** pure calculations, state transitions, input validation, time boundaries, and serializers.
2. **Contract tests:** every broker/provider adapter against a shared fake server or recorded fixtures.
3. **Component tests:** database migrations, data-health gate, reconciliation engine, control service.
4. **Scenario tests:** full paper-trading run with injected failures.
5. **Regression tests:** every defect gets a minimal test reproducer before the fix is accepted.

### Mandatory scenarios

- Stale quote, stale cache, missing SPY, malformed provider response, and data timeout.
- Market closed, unknown session, halted symbol, split/dividend/corporate-action flag, and earnings blackout.
- Soft freeze, hard lock, daily loss, weekly loss, consecutive-loss pause, and portfolio-risk breach.
- Broker accepts order, rejects order, times out after acceptance, partial fills, returns duplicate submission, and has an orphaned position.
- Process crash before submission, during submission, after submission, and before reconciliation.
- Native bracket accepted/rejected/missing; watchdog unavailable/degraded.
- Duplicate Telegram callback, unauthorized sender, expired approval, and user approval invalidated by later risk change.
- Backtest with gap stop, fees, slippage, delisting, and a held-out crisis regime.

## 9. Data and State Requirements

Schema work should introduce migrations rather than editing deployed database structure without a migration path. At minimum, add records for:

- `trading_runs`: run ID, mode, start/end, status, config hash, trigger, error, lease owner.
- `data_health`: provider, dataset, as-of timestamp, received timestamp, freshness threshold, status, error classification, source version.
- `operator_controls`: control type, status, actor ID, issued/expiry time, reason, audit ID.
- `order_intents`: immutable request hash, candidate/version references, risk decision, idempotency key, lifecycle timestamps.
- `broker_submissions`: intent reference, broker order ID, attempt, request/response redacted payload, outcome classification.
- `reconciliation_events`: local/broker snapshot hashes, mismatches, resolution status, timestamp.
- `protection_status`: position/order reference, native leg IDs, watchdog status, last verified time, degradation reason.
- `instrument_metadata`: sector, exchange, tradability, earnings/corporate-action dates, universe-version reference.
- `research_runs` and `data_manifests`: source hashes, date ranges, costs, universe version, strategy/config hash, results.

All state-changing operations must be transactional where possible and write an audit record containing a correlation ID.

## 10. Explicit Non-Goals

Do not include these in the current programme unless a human creates a new approved ADR:

- Real-money execution, real brokerage credentials, or live-account account reads.
- Leveraged products, options, CFDs, short selling, margin, crypto, or non-US securities.
- Automated parameter optimization that changes live/paper strategy configuration without review.
- Browser automation against broker websites.
- Replacing deterministic risk controls with an LLM or model score.
- Expanding scope into a general retail brokerage platform.

## 11. Final Integration Checklist

Before claiming the specification is implemented, the orchestrator must verify:

- [ ] All phase tickets are resolved with linked tests and resolution notes.
- [ ] `git diff --check` is clean.
- [ ] Full test suite, lint, type check, and migration test pass.
- [ ] Paper-only gate is tested and defaults to deny execution.
- [ ] No secret-like strings exist in tracked files or test output.
- [ ] Failure-injection scenarios in Section 8 pass.
- [ ] Dashboard/control-plane state is derived from durable records.
- [ ] A restart/reconciliation simulation completes without duplicate orders.
- [ ] Research reports are reproducible from manifests.
- [ ] `CONTEXT.md`, ADRs, README, and operational runbook match implemented behavior.

## 12. Orchestrator Handoff Format

At the end of each phase, report only:

```text
Phase: <ID and name>
Status: complete | blocked
Tickets: resolved IDs; remaining IDs
Validated: commands and outcome
Safety result: paper-only / fail-closed / reconciliation status
Notable decisions: ADRs or intentional deviations
Risks or blockers: concrete next action needed
```

If a phase is blocked, stop dependent work, preserve the working tree, record the blocker in the relevant `.scratch/` ticket, and request direction. Never “work around” a safety gate.
