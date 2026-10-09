# [29] Add an optional real provider adapter behind structured schemas, timeouts, cost limits, and cached replay

Type: task
Status: resolved
Blocked by: 04, 14

## Summary
Optional real provider adapter behind structured schemas, timeouts, cost limits, cached replay

## Context & Spec Reference
- Phase: Phase 6 — Decision Intelligence (Optional)
- Spec ID: P6-01
- Owner: LLM agent
- Allowed files: src/llm/*, tests/test_llm_provider_adapter.py
- Validation Command: `./.venv/bin/pytest tests/test_llm_provider_adapter.py`

## Acceptance Criteria
Invalid response, provider failure, and cost cap result in explicit no-trade/degraded behavior; no key appears in test output

## Comments
Implemented `LLMProviderAdapter`, `LLMProviderConfig`, `LLMCostTracker`, and `CircuitBreaker` in `src/llm/provider.py`. Enforces strict JSON schema validation for `AgentDeliberationOutput`; enforces daily cost capping ($1.00); trips circuit breaker on consecutive errors; fails closed to degraded no-trade/VETO output upon error or budget exhaustion; redacts all credentials and API tokens.

## Answer
Resolved. 7 tests passing in `tests/test_llm_provider_adapter.py`.
