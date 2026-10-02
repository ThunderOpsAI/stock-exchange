"""
Domain models and enumeration types for Autonomous Stock Exchange Trading System.
Adheres strictly to CONTEXT.md invariants and research specifications.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


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


# --- Domain Entity Models ---

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
