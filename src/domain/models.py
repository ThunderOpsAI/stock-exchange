"""
Domain models and enumeration types for Autonomous Stock Exchange Trading System.
Adheres strictly to CONTEXT.md invariants and research specifications.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    BRACKET = "BRACKET"


class OrderState(str, Enum):
    PENDING_RISK_CHECK = "PENDING_RISK_CHECK"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    ROUTED_TO_BROKER = "ROUTED_TO_BROKER"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED_BY_BROKER = "REJECTED_BY_BROKER"
    EXPIRED = "EXPIRED"


class StrategyType(str, Enum):
    TREND_PULLBACK = "TREND_PULLBACK"
    MEAN_REVERSION = "MEAN_REVERSION"


class CandidateStatus(str, Enum):
    PENDING_DELIBERATION = "PENDING_DELIBERATION"
    APPROVED = "APPROVED"
    VETOED = "VETOED"
    HITL_ESCALATED = "HITL_ESCALATED"
    EXPIRED = "EXPIRED"


class AgentRole(str, Enum):
    SENTIMENT_CATALYST = "sentiment_catalyst"
    TECHNICAL_STRUCTURE = "technical_structure"
    ADVERSARIAL_RISK = "adversarial_risk"


class AgentStance(str, Enum):
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"
    VETO = "VETO"


class VerdictOutcome(str, Enum):
    AUTO_APPROVED = "AUTO_APPROVED"
    HITL_ESCALATED = "HITL_ESCALATED"
    AUTO_DROPPED = "AUTO_DROPPED"


class HITLStatus(str, Enum):
    PENDING_TELEGRAM_RESPONSE = "PENDING_TELEGRAM_RESPONSE"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    HUMAN_REJECTED = "HUMAN_REJECTED"
    TIMED_OUT = "TIMED_OUT"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class ExitReason(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    GAP_STOP = "GAP_STOP"
    TIME_STOP = "TIME_STOP"
    TRAILING_STOP = "TRAILING_STOP"
    EARNINGS_PRE_EXIT = "EARNINGS_PRE_EXIT"
    CIRCUIT_BREAKER_HALT = "CIRCUIT_BREAKER_HALT"
    MANUAL_CLOSE = "MANUAL_CLOSE"


class CircuitBreakerTier(int, Enum):
    NORMAL = 0
    SOFT_HALT = 1  # Equity <= $80.00
    HARD_LIQUIDATION = 2  # Equity <= $70.00


class AuditSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class DataFreshness(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNHEALTHY = "UNHEALTHY"
    ERROR = "ERROR"


class MarketDataHealth(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    ERROR = "ERROR"


class DataIntegrityError(Exception):
    """Raised when market data fails integrity, freshness, or schema validation."""
    pass


class MarketSessionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    PRE_MARKET = "PRE_MARKET"
    AFTER_HOURS = "AFTER_HOURS"
    HALTED = "HALTED"
    UNKNOWN = "UNKNOWN"


class GateCheckResult(BaseModel):
    passed: bool
    gate_name: str
    session_status: MarketSessionStatus
    reason: str
    ticker: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# --- Domain Entity Models ---

class DataProvenance(BaseModel):
    source: str
    ticker: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    as_of_time: Optional[datetime] = None
    status: DataFreshness = DataFreshness.FRESH
    latency_ms: Optional[float] = None
    cache_hit: bool = False
    details: Optional[str] = None


class ExecutionQuote(BaseModel):
    """
    Real-time execution quote with bid/ask prices, timestamp, and relative spread.
    Separates live execution pricing from historical daily bars (ADR 0002).
    """
    ticker: str
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    spread_usd: float = 0.0
    spread_rel: float = 0.0
    spread_bps: float = 0.0
    is_valid: bool = True

    @model_validator(mode="after")
    def compute_spread_and_validate(self) -> ExecutionQuote:
        if not self.is_valid or self.bid <= 0.0 or self.ask < self.bid:
            self.is_valid = False
            self.spread_usd = round(self.ask - self.bid, 6)
            self.spread_rel = 0.0
            self.spread_bps = 0.0
            return self

        diff = self.ask - self.bid
        self.spread_usd = round(diff, 6)
        if self.ask > 0.0:
            self.spread_rel = diff / self.ask
            self.spread_bps = self.spread_rel * 10000.0
        else:
            self.spread_rel = 0.0
            self.spread_bps = 0.0
            self.is_valid = False
        return self

    def validate_quote(self, max_age_seconds: Optional[float] = None) -> Tuple[bool, str]:
        """Validates quote pricing integrity and optional freshness."""
        if not self.is_valid:
            return False, f"Invalid quote: bid={self.bid}, ask={self.ask}"
        if self.bid <= 0.0:
            return False, f"Non-positive bid price: ${self.bid}"
        if self.ask < self.bid:
            return False, f"Inverted quote: ask (${self.ask}) < bid (${self.bid})"
        if max_age_seconds is not None:
            now = datetime.now(timezone.utc)
            ts = self.timestamp if self.timestamp.tzinfo else self.timestamp.replace(tzinfo=timezone.utc)
            age = (now - ts).total_seconds()
            if age > max_age_seconds:
                return False, f"Quote is stale: age {age:.1f}s > {max_age_seconds:.1f}s"
            if age < -10.0:
                return False, f"Quote timestamp is in future: {self.timestamp}"
        return True, "Quote is valid"


QuoteData = ExecutionQuote


class MarketDataResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ticker: str
    status: MarketDataHealth
    freshness: DataFreshness
    provenance: DataProvenance
    df: Optional[Any] = None
    bars_count: int = 0
    quote: Optional[ExecutionQuote] = None
    error_reason: Optional[str] = None
    error_classification: Optional[str] = None


class MarketSnapshot(BaseModel):
    snapshot_id: Optional[int] = None
    timestamp: datetime
    ticker: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    rsi_14: Optional[float] = None
    ema_20: Optional[float] = None
    sma_50: Optional[float] = None
    sma_200: Optional[float] = None
    atr_14: Optional[float] = None
    rs_spy_63d: Optional[float] = None
    rvol_20: Optional[float] = None
    spread_bps: Optional[float] = None
    created_at: Optional[datetime] = None


class ScreenedCandidate(BaseModel):
    candidate_id: str
    timestamp: datetime
    ticker: str
    strategy: StrategyType
    entry_est: float
    stop_loss: float
    take_profit: float
    risk_r: float
    allocated_usd: float
    rank_score: float
    status: CandidateStatus = CandidateStatus.PENDING_DELIBERATION
    created_at: Optional[datetime] = None


class AgentDeliberationOutput(BaseModel):
    agent_role: AgentRole
    model_name: str
    stance: AgentStance
    score_10: float = Field(ge=0.0, le=10.0)
    bullish_catalysts: List[str] = Field(default_factory=list)
    risk_factors: List[str] = Field(default_factory=list)
    rationale_summary: str


class LLMDeliberation(BaseModel):
    deliberation_id: str
    candidate_id: str
    ticker: str
    agent_role: AgentRole
    model_name: str
    stance: AgentStance
    score_10: float
    bullish_catalysts: List[str] = Field(default_factory=list)
    risk_factors: List[str] = Field(default_factory=list)
    rationale_summary: str
    token_cost_usd: float = 0.0
    created_at: Optional[datetime] = None


class CommitteeVerdict(BaseModel):
    verdict_id: str
    candidate_id: str
    ticker: str
    timestamp: datetime
    composite_score: float
    verdict_outcome: VerdictOutcome
    risk_officer_dissent: bool = False
    hitl_status: Optional[HITLStatus] = None
    hitl_responded_at: Optional[datetime] = None
    telegram_message_id: Optional[int] = None
    created_at: Optional[datetime] = None


class OrderRequest(BaseModel):
    ticker: str
    side: OrderSide
    dollar_amount: float
    target_qty: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    client_order_id: str = Field(default_factory=lambda: f"ord_{int(datetime.now(timezone.utc).timestamp() * 1000)}")
    candidate_id: Optional[str] = None


class OrderResult(BaseModel):
    order_id: str
    client_order_id: str
    ticker: str
    status: OrderState
    filled_price: Optional[float] = None
    filled_qty: Optional[float] = None
    filled_dollar_amount: Optional[float] = None
    fee: float = 0.0
    slippage: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    error_message: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None


class Order(BaseModel):
    order_id: str
    client_order_id: str
    candidate_id: Optional[str] = None
    ticker: str
    side: OrderSide
    order_type: OrderType
    allocated_usd: float
    target_qty: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    state: OrderState = OrderState.PENDING_RISK_CHECK
    rejection_reason: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Fill(BaseModel):
    fill_id: str
    order_id: str
    broker_order_id: Optional[str] = None
    ticker: str
    side: OrderSide
    filled_qty: float
    filled_price: float
    filled_notional: float
    broker_fee_usd: float = 0.0
    slippage_usd: float = 0.0
    executed_at: datetime


class Position(BaseModel):
    position_id: str
    broker_position_id: Optional[str] = None
    ticker: str
    side: OrderSide
    qty: float
    entry_price: float
    current_price: float
    stop_loss: float
    take_profit: float
    market_value: float
    unrealized_pnl: float
    status: PositionStatus = PositionStatus.OPEN
    opened_at: datetime
    closed_at: Optional[datetime] = None
    realized_pnl: float = 0.0
    exit_reason: Optional[ExitReason] = None


class AccountBalance(BaseModel):
    cash: float
    equity: float
    buying_power: float
    currency: str = "USD"
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PortfolioSnapshot(BaseModel):
    snapshot_id: Optional[int] = None
    timestamp: datetime
    total_equity: float
    cash_balance: float
    invested_capital: float
    unrealized_pnl: float
    active_slots_used: int
    circuit_breaker_tier: CircuitBreakerTier = CircuitBreakerTier.NORMAL
    created_at: Optional[datetime] = None


class AuditLog(BaseModel):
    log_id: Optional[int] = None
    timestamp: datetime
    severity: AuditSeverity
    component: str
    event_name: str
    message: str
    metadata: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None


class DeliberationCacheEntry(BaseModel):
    cache_key: str
    symbol: str
    as_of_date: str
    strategy_id: Optional[str] = None
    agent_role: str
    prompt_hash: Optional[str] = None
    model_name: Optional[str] = None
    response_json: str
    created_at: Optional[datetime] = None


# --- Phase 2: Execution, Run Lifecycle & State Transitions ---

class IllegalStateTransitionError(Exception):
    """Raised when an illegal lifecycle state transition is attempted."""
    pass


class TradingRunStatus(str, Enum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class OrderIntentStatus(str, Enum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    RECONCILED = "RECONCILED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN_PENDING_RECONCILIATION = "UNKNOWN_PENDING_RECONCILIATION"


class BrokerSubmissionOutcome(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN_PENDING_RECONCILIATION = "UNKNOWN_PENDING_RECONCILIATION"
    NETWORK_ERROR = "NETWORK_ERROR"


class ReconciliationStatus(str, Enum):
    HEALTHY_MATCH = "HEALTHY_MATCH"
    RESOLVED_RECONCILED = "RESOLVED_RECONCILED"
    UNRESOLVED_DISCREPANCY = "UNRESOLVED_DISCREPANCY"
    REVERTED_ORPHAN = "REVERTED_ORPHAN"


class ProtectionMode(str, Enum):
    NATIVE_BRACKET = "NATIVE_BRACKET"
    WATCHDOG_SOFTWARE = "WATCHDOG_SOFTWARE"
    DEGRADED_UNPROTECTED = "DEGRADED_UNPROTECTED"


class TradingRun(BaseModel):
    run_id: str
    mode: str = "paper"
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: Optional[datetime] = None
    status: TradingRunStatus = TradingRunStatus.STARTING
    config_hash: str
    trigger: str = "cli"
    error_message: Optional[str] = None
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[datetime] = None

    _LEGAL_TRANSITIONS = {
        TradingRunStatus.STARTING: {
            TradingRunStatus.RUNNING,
            TradingRunStatus.FAILED,
            TradingRunStatus.INTERRUPTED,
        },
        TradingRunStatus.RUNNING: {
            TradingRunStatus.COMPLETED,
            TradingRunStatus.FAILED,
            TradingRunStatus.INTERRUPTED,
        },
        TradingRunStatus.COMPLETED: set(),
        TradingRunStatus.FAILED: set(),
        TradingRunStatus.INTERRUPTED: set(),
    }

    def can_transition_to(self, new_status: TradingRunStatus) -> bool:
        return new_status in self._LEGAL_TRANSITIONS.get(self.status, set())

    def transition_to(
        self,
        new_status: TradingRunStatus,
        error_message: Optional[str] = None,
        ended_at: Optional[datetime] = None,
    ) -> None:
        if not self.can_transition_to(new_status):
            raise IllegalStateTransitionError(
                f"Cannot transition TradingRun from {self.status.value} to {new_status.value}"
            )
        self.status = new_status
        if error_message is not None:
            self.error_message = error_message
        if new_status in (TradingRunStatus.COMPLETED, TradingRunStatus.FAILED, TradingRunStatus.INTERRUPTED):
            self.ended_at = ended_at or datetime.now(timezone.utc)


class OrderIntent(BaseModel):
    intent_id: str
    request_hash: str
    candidate_id: Optional[str] = None
    ticker: str
    side: OrderSide
    target_qty: float
    allocated_usd: float
    idempotency_key: str
    risk_decision: str = "APPROVED"
    risk_reason: Optional[str] = None
    limit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    status: OrderIntentStatus = OrderIntentStatus.CREATED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    _LEGAL_TRANSITIONS = {
        OrderIntentStatus.CREATED: {
            OrderIntentStatus.SUBMITTED,
            OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION,
            OrderIntentStatus.REJECTED,
            OrderIntentStatus.EXPIRED,
        },
        OrderIntentStatus.SUBMITTED: {
            OrderIntentStatus.RECONCILED,
            OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION,
            OrderIntentStatus.REJECTED,
        },
        OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION: {
            OrderIntentStatus.RECONCILED,
            OrderIntentStatus.REJECTED,
        },
        OrderIntentStatus.RECONCILED: set(),
        OrderIntentStatus.REJECTED: set(),
        OrderIntentStatus.EXPIRED: set(),
    }

    def can_transition_to(self, new_status: OrderIntentStatus) -> bool:
        return new_status in self._LEGAL_TRANSITIONS.get(self.status, set())

    def transition_to(
        self,
        new_status: OrderIntentStatus,
        reason: Optional[str] = None,
    ) -> None:
        if not self.can_transition_to(new_status):
            raise IllegalStateTransitionError(
                f"Cannot transition OrderIntent from {self.status.value} to {new_status.value}"
            )
        self.status = new_status
        self.updated_at = datetime.now(timezone.utc)
        if reason:
            self.risk_reason = f"{self.risk_reason}; {reason}" if self.risk_reason else reason


class BrokerSubmission(BaseModel):
    submission_id: str
    intent_id: str
    broker_order_id: Optional[str] = None
    attempt_number: int = 1
    client_order_id: str
    request_payload_redacted: Optional[str] = None
    response_payload_redacted: Optional[str] = None
    outcome_classification: BrokerSubmissionOutcome
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReconciliationRecord(BaseModel):
    event_id: str
    run_id: Optional[str] = None
    local_snapshot_hash: str
    broker_snapshot_hash: str
    mismatches_json: str = "[]"
    resolution_status: ReconciliationStatus
    resolution_notes: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProtectionStatusRecord(BaseModel):
    protection_id: str
    position_id: Optional[str] = None
    order_id: Optional[str] = None
    ticker: str
    protection_mode: ProtectionMode
    stop_loss_order_id: Optional[str] = None
    take_profit_order_id: Optional[str] = None
    watchdog_healthy: int = 1
    last_verified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    degradation_reason: Optional[str] = None

    def is_degraded(self) -> bool:
        return (
            self.protection_mode == ProtectionMode.DEGRADED_UNPROTECTED
            or self.watchdog_healthy == 0
        )


class InstrumentMetadata(BaseModel):
    ticker: str
    exchange: str
    universe_version: str
    sector: Optional[str] = None
    industry: Optional[str] = None
    tradable: int = 1
    min_lot_size: float = 1.0
    price_increment: float = 0.01
    next_earnings_date: Optional[datetime] = None
    corporate_action_flag: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
