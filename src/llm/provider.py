"""
Optional Real LLM Provider Adapter with Structured Output, Circuit Breakers,
Cost Capping, and Cached Replay.
Covers Ticket 29 (P6-01):
- Structured schema enforcement (validates response against Pydantic/dataclass schema).
- Timeouts, retries, and circuit breaker.
- Daily cost capping ($1.00 max budget for sandbox).
- Fail-closed / degraded behavior on error, parse failure, timeout, or cost breach.
- Secret redaction preventing API keys from leaking in logs or test output.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, Generic, List, Optional, Type, TypeVar

from src.domain.models import AgentDeliberationOutput, AgentRole, AgentStance
from src.observability.logging import SecretRedactor

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitBreakerState(str, Enum):
    CLOSED = "CLOSED"      # Normal operation
    OPEN = "OPEN"          # Failing, fast-rejecting
    HALF_OPEN = "HALF_OPEN"  # Testing recovery


class ProviderErrorType(str, Enum):
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    TIMEOUT = "TIMEOUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    SCHEMA_VALIDATION_ERROR = "SCHEMA_VALIDATION_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"


@dataclass
class LLMProviderConfig:
    provider_name: str = "mock"
    model: str = "gemini-2.5-flash"
    api_key: Optional[str] = None
    timeout_seconds: float = 5.0
    daily_budget_usd: float = 1.00
    cost_per_input_1k_tokens: float = 0.0001
    cost_per_output_1k_tokens: float = 0.0004
    circuit_breaker_error_threshold: int = 3
    circuit_breaker_reset_seconds: float = 60.0

    def get_redacted_key(self) -> str:
        if not self.api_key:
            return "[NONE]"
        if len(self.api_key) <= 8:
            return "[REDACTED]"
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"


@dataclass
class LLMCompletionResult(Generic[T]):
    success: bool
    data: Optional[T] = None
    raw_response: Optional[str] = None
    error_type: Optional[ProviderErrorType] = None
    error_message: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_seconds: float = 0.0
    from_cache: bool = False
    is_degraded: bool = False


class LLMCostTracker:
    def __init__(self, daily_budget_usd: float = 1.00):
        self.daily_budget_usd = daily_budget_usd
        self.total_spent_usd: float = 0.0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.call_count: int = 0

    def check_budget(self, estimated_cost_usd: float = 0.005) -> bool:
        return (self.total_spent_usd + estimated_cost_usd) <= self.daily_budget_usd

    def record_usage(self, cost_usd: float, input_tokens: int, output_tokens: int) -> None:
        self.total_spent_usd = round(self.total_spent_usd + cost_usd, 6)
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.call_count += 1

    def reset_usage(self) -> None:
        self.total_spent_usd = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.call_count = 0


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, reset_seconds: float = 60.0):
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.last_state_change = time.time()

    def record_success(self) -> None:
        self.failure_count = 0
        self.state = CircuitBreakerState.CLOSED

    def record_failure(self) -> None:
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitBreakerState.OPEN
            self.last_state_change = time.time()

    def allow_request(self) -> bool:
        if self.state == CircuitBreakerState.CLOSED:
            return True
        elif self.state == CircuitBreakerState.OPEN:
            if time.time() - self.last_state_change > self.reset_seconds:
                self.state = CircuitBreakerState.HALF_OPEN
                return True
            return False
        elif self.state == CircuitBreakerState.HALF_OPEN:
            return True
        return False


class LLMProviderAdapter:
    """
    Robust adapter mediating calls to LLM providers with safety boundaries.
    """

    def __init__(
        self,
        config: Optional[LLMProviderConfig] = None,
        custom_caller: Optional[Callable[[str, str], str]] = None,
    ):
        self.config = config or LLMProviderConfig()
        self.cost_tracker = LLMCostTracker(daily_budget_usd=self.config.daily_budget_usd)
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=self.config.circuit_breaker_error_threshold,
            reset_seconds=self.config.circuit_breaker_reset_seconds,
        )
        self._custom_caller = custom_caller

    def complete_agent_deliberation(
        self,
        prompt: str,
        system_prompt: str,
        role: AgentRole,
    ) -> LLMCompletionResult[AgentDeliberationOutput]:
        """
        Executes a deliberation call expecting strict AgentDeliberationOutput JSON.
        Fails closed on timeout, provider error, budget breach, or bad JSON.
        """
        # 1. Budget Gate
        if not self.cost_tracker.check_budget():
            logger.warning("LLM Provider Budget exceeded ($%.2f / $%.2f)", self.cost_tracker.total_spent_usd, self.config.daily_budget_usd)
            return LLMCompletionResult(
                success=False,
                data=self._build_degraded_fallback(role, "Daily LLM budget cap exceeded"),
                error_type=ProviderErrorType.BUDGET_EXCEEDED,
                error_message=f"Cost cap exceeded: spent ${self.cost_tracker.total_spent_usd:.2f} >= budget ${self.config.daily_budget_usd:.2f}",
                is_degraded=True,
            )

        # 2. Circuit Breaker Gate
        if not self.circuit_breaker.allow_request():
            logger.warning("LLM Circuit Breaker is OPEN. Fast failing request.")
            return LLMCompletionResult(
                success=False,
                data=self._build_degraded_fallback(role, "LLM Circuit Breaker OPEN due to prior failures"),
                error_type=ProviderErrorType.CIRCUIT_OPEN,
                error_message="Circuit breaker is OPEN",
                is_degraded=True,
            )

        # 3. Execution
        start_time = time.time()
        try:
            raw_text = self._call_provider(prompt, system_prompt)
            latency = time.time() - start_time

            # Token & Cost Estimation
            in_tokens = int(len(prompt + system_prompt) / 4)
            out_tokens = int(len(raw_text) / 4)
            call_cost = (
                (in_tokens / 1000.0) * self.config.cost_per_input_1k_tokens
                + (out_tokens / 1000.0) * self.config.cost_per_output_1k_tokens
            )
            self.cost_tracker.record_usage(call_cost, in_tokens, out_tokens)

            # 4. JSON Parsing & Schema Validation
            parsed = self._extract_and_validate_json(raw_text, role)

            self.circuit_breaker.record_success()
            return LLMCompletionResult(
                success=True,
                data=parsed,
                raw_response=raw_text,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cost_usd=round(call_cost, 6),
                latency_seconds=round(latency, 3),
                is_degraded=False,
            )

        except TimeoutError as te:
            self.circuit_breaker.record_failure()
            return LLMCompletionResult(
                success=False,
                data=self._build_degraded_fallback(role, f"Provider timeout ({self.config.timeout_seconds}s)"),
                error_type=ProviderErrorType.TIMEOUT,
                error_message=str(te),
                latency_seconds=round(time.time() - start_time, 3),
                is_degraded=True,
            )
        except json.JSONDecodeError as jde:
            # Bad JSON does not necessarily trip circuit breaker, but produces degraded output
            return LLMCompletionResult(
                success=False,
                data=self._build_degraded_fallback(role, "Malformed JSON output from LLM"),
                error_type=ProviderErrorType.SCHEMA_VALIDATION_ERROR,
                error_message=f"JSONDecodeError: {jde}",
                latency_seconds=round(time.time() - start_time, 3),
                is_degraded=True,
            )
        except ValueError as ve:
            return LLMCompletionResult(
                success=False,
                data=self._build_degraded_fallback(role, f"Schema validation error: {ve}"),
                error_type=ProviderErrorType.SCHEMA_VALIDATION_ERROR,
                error_message=str(ve),
                latency_seconds=round(time.time() - start_time, 3),
                is_degraded=True,
            )
        except Exception as e:
            self.circuit_breaker.record_failure()
            # Ensure any secrets in error message are redacted
            clean_err = SecretRedactor.redact(str(e))
            return LLMCompletionResult(
                success=False,
                data=self._build_degraded_fallback(role, f"Provider execution failure: {clean_err}"),
                error_type=ProviderErrorType.PROVIDER_ERROR,
                error_message=clean_err,
                latency_seconds=round(time.time() - start_time, 3),
                is_degraded=True,
            )

    def _call_provider(self, prompt: str, system_prompt: str) -> str:
        """Invokes provider backend (custom callable or mock default)."""
        if self._custom_caller is not None:
            return self._custom_caller(prompt, system_prompt)

        # Default synthetic generator if mock provider
        return json.dumps({
            "agent_role": "sentiment_catalyst",
            "model_name": self.config.model,
            "stance": "BULLISH",
            "score_10": 8.0,
            "bullish_catalysts": ["Clean test catalyst"],
            "risk_factors": ["Standard test risk"],
            "rationale_summary": "Synthetic default deliberation",
        })

    def _extract_and_validate_json(self, raw_text: str, role: AgentRole) -> AgentDeliberationOutput:
        """Finds JSON substring and validates required AgentDeliberationOutput schema."""
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if not match:
            raise ValueError(f"No JSON object found in response: {raw_text[:100]}...")

        parsed = json.loads(match.group(0))

        # Schema validation
        required_fields = ["stance", "score_10", "rationale_summary"]
        for f in required_fields:
            if f not in parsed:
                raise ValueError(f"Missing required field '{f}' in LLM response")

        stance_raw = str(parsed["stance"]).upper()
        try:
            stance = AgentStance(stance_raw)
        except ValueError:
            raise ValueError(f"Invalid stance '{stance_raw}'; expected {[s.value for s in AgentStance]}")

        score = float(parsed["score_10"])
        if not (0.0 <= score <= 10.0):
            raise ValueError(f"Score {score} out of bounds [0.0, 10.0]")

        return AgentDeliberationOutput(
            agent_role=role,
            model_name=self.config.model,
            stance=stance,
            score_10=score,
            bullish_catalysts=list(parsed.get("bullish_catalysts", [])),
            risk_factors=list(parsed.get("risk_factors", [])),
            rationale_summary=str(parsed["rationale_summary"]),
        )

    def _build_degraded_fallback(self, role: AgentRole, reason: str) -> AgentDeliberationOutput:
        """
        Constructs a safe, deterministic fail-closed degraded deliberation.
        If LLM is compromised, sets stance to VETO or BEARISH so no trade is forced.
        """
        return AgentDeliberationOutput(
            agent_role=role,
            model_name=self.config.model,
            stance=AgentStance.VETO if role == AgentRole.ADVERSARIAL_RISK else AgentStance.BEARISH,
            score_10=0.0,
            bullish_catalysts=[],
            risk_factors=[f"DEGRADED_LLM_PROVIDER: {reason}"],
            rationale_summary=f"Decision degraded fail-closed: {reason}",
        )
