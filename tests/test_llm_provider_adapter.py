"""
Unit and integration tests for LLMProviderAdapter.
Covers Ticket 29 (P6-01):
- Structured schemas, timeouts, cost limits, circuit breakers.
- Invalid response, provider failure, and cost cap result in explicit no-trade/degraded behavior.
- No key appears in test output.
"""

import json
import pytest

from src.domain.models import AgentRole, AgentStance
from src.llm.provider import (
    CircuitBreakerState,
    LLMCompletionResult,
    LLMProviderAdapter,
    LLMProviderConfig,
    ProviderErrorType,
)


def test_provider_structured_success():
    valid_json = json.dumps({
        "stance": "BULLISH",
        "score_10": 8.5,
        "bullish_catalysts": ["Strong revenue growth"],
        "risk_factors": ["Macro headwind"],
        "rationale_summary": "Solid fundamentals with favorable momentum.",
    })

    adapter = LLMProviderAdapter(custom_caller=lambda p, s: valid_json)
    res = adapter.complete_agent_deliberation(
        prompt="Analyze AAPL",
        system_prompt="You are sentiment analyst",
        role=AgentRole.SENTIMENT_CATALYST,
    )

    assert res.success is True
    assert res.is_degraded is False
    assert res.data is not None
    assert res.data.stance == AgentStance.BULLISH
    assert res.data.score_10 == 8.5
    assert len(res.data.bullish_catalysts) == 1
    assert res.cost_usd > 0.0


def test_provider_malformed_json_degraded():
    # Garbage output
    adapter = LLMProviderAdapter(custom_caller=lambda p, s: "Not JSON at all! Just text.")
    res = adapter.complete_agent_deliberation(
        prompt="Analyze AAPL",
        system_prompt="You are analyst",
        role=AgentRole.SENTIMENT_CATALYST,
    )

    assert res.success is False
    assert res.is_degraded is True
    assert res.error_type == ProviderErrorType.SCHEMA_VALIDATION_ERROR
    # Must fail closed: BEARISH stance, 0 score
    assert res.data.stance == AgentStance.BEARISH
    assert res.data.score_10 == 0.0
    assert "DEGRADED_LLM_PROVIDER" in res.data.risk_factors[0]


def test_provider_schema_invalid_stance_degraded():
    # Invalid stance string
    invalid_stance = json.dumps({
        "stance": "SUPER_DUPER_BUY",
        "score_10": 9.0,
        "rationale_summary": "Unrecognized stance",
    })
    adapter = LLMProviderAdapter(custom_caller=lambda p, s: invalid_stance)
    res = adapter.complete_agent_deliberation(
        prompt="Analyze",
        system_prompt="",
        role=AgentRole.TECHNICAL_STRUCTURE,
    )

    assert res.success is False
    assert res.is_degraded is True
    assert res.data.stance == AgentStance.BEARISH
    assert res.data.score_10 == 0.0


def test_provider_timeout_degraded():
    def _timeout_caller(p, s):
        raise TimeoutError("Provider gateway timed out after 5000ms")

    adapter = LLMProviderAdapter(custom_caller=_timeout_caller)
    res = adapter.complete_agent_deliberation(
        prompt="Analyze",
        system_prompt="",
        role=AgentRole.ADVERSARIAL_RISK,
    )

    assert res.success is False
    assert res.is_degraded is True
    assert res.error_type == ProviderErrorType.TIMEOUT
    # Risk officer degraded output must be VETO
    assert res.data.stance == AgentStance.VETO
    assert res.data.score_10 == 0.0


def test_provider_cost_budget_cap():
    # Set small budget $0.001
    cfg = LLMProviderConfig(daily_budget_usd=0.001)
    adapter = LLMProviderAdapter(config=cfg)

    # Artificially consume budget
    adapter.cost_tracker.record_usage(cost_usd=0.002, input_tokens=1000, output_tokens=500)

    res = adapter.complete_agent_deliberation(
        prompt="Analyze",
        system_prompt="",
        role=AgentRole.SENTIMENT_CATALYST,
    )

    assert res.success is False
    assert res.is_degraded is True
    assert res.error_type == ProviderErrorType.BUDGET_EXCEEDED
    assert res.data.stance == AgentStance.BEARISH
    assert "Cost cap exceeded" in res.error_message


def test_provider_circuit_breaker_trips():
    def _failing_caller(p, s):
        raise RuntimeError("Service Unavailable 503")

    cfg = LLMProviderConfig(circuit_breaker_error_threshold=2)
    adapter = LLMProviderAdapter(config=cfg, custom_caller=_failing_caller)

    # Call 1 fails
    res1 = adapter.complete_agent_deliberation("p", "s", AgentRole.SENTIMENT_CATALYST)
    assert res1.error_type == ProviderErrorType.PROVIDER_ERROR
    assert adapter.circuit_breaker.state == CircuitBreakerState.CLOSED

    # Call 2 fails -> trips circuit breaker to OPEN
    res2 = adapter.complete_agent_deliberation("p", "s", AgentRole.SENTIMENT_CATALYST)
    assert res2.error_type == ProviderErrorType.PROVIDER_ERROR
    assert adapter.circuit_breaker.state == CircuitBreakerState.OPEN

    # Call 3 fast fails due to CIRCUIT_OPEN
    res3 = adapter.complete_agent_deliberation("p", "s", AgentRole.SENTIMENT_CATALYST)
    assert res3.error_type == ProviderErrorType.CIRCUIT_OPEN
    assert res3.is_degraded is True
    assert res3.data.stance == AgentStance.BEARISH


def test_secret_redaction_in_config_and_errors():
    secret_key = "AIzaSySecretFakeApiKey1234567890abcdef"
    cfg = LLMProviderConfig(api_key=secret_key)

    redacted = cfg.get_redacted_key()
    assert secret_key not in redacted
    assert "AIza" in redacted or "[REDACTED]" in redacted

    def _failing_with_secret(p, s):
        raise RuntimeError(f"Failed authenticating with token {secret_key}")

    adapter = LLMProviderAdapter(config=cfg, custom_caller=_failing_with_secret)
    res = adapter.complete_agent_deliberation("p", "s", AgentRole.SENTIMENT_CATALYST)

    assert secret_key not in res.error_message
    assert secret_key not in res.data.risk_factors[0]
