"""
Unit tests for SQLite persistence layer and domain data models.
Validates CRUD operations, WAL mode, migrations, and foreign key constraints.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timezone
import pytest

pytestmark = pytest.mark.integration

from src.domain.models import (
    AgentRole,
    AgentStance,
    AuditSeverity,
    CandidateStatus,
    CircuitBreakerTier,
    CommitteeVerdict,
    DeliberationCacheEntry,
    ExitReason,
    Fill,
    HITLStatus,
    LLMDeliberation,
    MarketSnapshot,
    Order,
    OrderSide,
    OrderState,
    OrderType,
    PortfolioSnapshot,
    Position,
    PositionStatus,
    ScreenedCandidate,
    StrategyType,
    VerdictOutcome,
)
from src.storage.db import Database


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(db_path=path)
    yield db
    if os.path.exists(path):
        os.remove(path)
    wal_file = f"{path}-wal"
    shm_file = f"{path}-shm"
    if os.path.exists(wal_file):
        os.remove(wal_file)
    if os.path.exists(shm_file):
        os.remove(shm_file)


def test_wal_mode_and_foreign_keys(temp_db):
    with temp_db.session() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert journal_mode.lower() == "wal"
        foreign_keys = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert foreign_keys == 1


def test_market_snapshot_crud(temp_db):
    now = datetime.now(timezone.utc)
    snap = MarketSnapshot(
        timestamp=now,
        ticker="AAPL",
        open=150.0,
        high=155.0,
        low=149.0,
        close=154.0,
        volume=50000000.0,
        rsi_14=45.5,
        ema_20=151.2,
        sma_50=148.0,
        sma_200=140.0,
        atr_14=2.5,
        rs_spy_63d=1.08,
        rvol_20=1.35,
        spread_bps=3.2,
    )
    row_id = temp_db.save_market_snapshot(snap)
    assert row_id is not None

    retrieved = temp_db.get_latest_market_snapshot("AAPL")
    assert retrieved is not None
    assert retrieved.ticker == "AAPL"
    assert retrieved.close == 154.0
    assert retrieved.rvol_20 == 1.35


def test_candidate_and_foreign_key_constraint(temp_db):
    now = datetime.now(timezone.utc)
    candidate = ScreenedCandidate(
        candidate_id="cand_aapl_001",
        timestamp=now,
        ticker="AAPL",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=150.0,
        stop_loss=146.0,
        take_profit=158.0,
        risk_r=4.0,
        allocated_usd=28.5,
        rank_score=1.85,
        status=CandidateStatus.PENDING_DELIBERATION,
    )
    temp_db.save_candidate(candidate)

    retrieved = temp_db.get_candidate("cand_aapl_001")
    assert retrieved is not None
    assert retrieved.status == CandidateStatus.PENDING_DELIBERATION

    temp_db.update_candidate_status("cand_aapl_001", CandidateStatus.APPROVED)
    retrieved_updated = temp_db.get_candidate("cand_aapl_001")
    assert retrieved_updated.status == CandidateStatus.APPROVED

    # Foreign key test on LLMDeliberation: non-existent candidate must fail
    fake_delib = LLMDeliberation(
        deliberation_id="delib_fake",
        candidate_id="non_existent_candidate",
        ticker="AAPL",
        agent_role=AgentRole.SENTIMENT_CATALYST,
        model_name="mock-gemini",
        stance=AgentStance.BULLISH,
        score_10=8.5,
        bullish_catalysts=["Earnings surprise"],
        risk_factors=[],
        rationale_summary="Strong catalyst.",
    )
    with pytest.raises(sqlite3.IntegrityError):
        temp_db.save_llm_deliberation(fake_delib)

    # Valid candidate deliberation
    valid_delib = LLMDeliberation(
        deliberation_id="delib_valid_001",
        candidate_id="cand_aapl_001",
        ticker="AAPL",
        agent_role=AgentRole.SENTIMENT_CATALYST,
        model_name="mock-gemini",
        stance=AgentStance.BULLISH,
        score_10=8.5,
        bullish_catalysts=["Earnings beat", "Guidance raise"],
        risk_factors=["Tech valuation high"],
        rationale_summary="Strong bullish thesis.",
        token_cost_usd=0.002,
    )
    temp_db.save_llm_deliberation(valid_delib)
    delibs = temp_db.get_deliberations_for_candidate("cand_aapl_001")
    assert len(delibs) == 1
    assert delibs[0].score_10 == 8.5
    assert "Earnings beat" in delibs[0].bullish_catalysts


def test_verdict_and_hitl_update(temp_db):
    now = datetime.now(timezone.utc)
    candidate = ScreenedCandidate(
        candidate_id="cand_msft_001",
        timestamp=now,
        ticker="MSFT",
        strategy=StrategyType.MEAN_REVERSION,
        entry_est=300.0,
        stop_loss=294.0,
        take_profit=309.0,
        risk_r=6.0,
        allocated_usd=29.0,
        rank_score=1.45,
        status=CandidateStatus.HITL_ESCALATED,
    )
    temp_db.save_candidate(candidate)

    verdict = CommitteeVerdict(
        verdict_id="verd_msft_001",
        candidate_id="cand_msft_001",
        ticker="MSFT",
        timestamp=now,
        composite_score=68.5,
        verdict_outcome=VerdictOutcome.HITL_ESCALATED,
        risk_officer_dissent=True,
        hitl_status=HITLStatus.PENDING_TELEGRAM_RESPONSE,
        telegram_message_id=999888,
    )
    temp_db.save_committee_verdict(verdict)

    retrieved = temp_db.get_committee_verdict("cand_msft_001")
    assert retrieved is not None
    assert retrieved.risk_officer_dissent is True
    assert retrieved.hitl_status == HITLStatus.PENDING_TELEGRAM_RESPONSE

    temp_db.update_hitl_verdict("cand_msft_001", HITLStatus.HUMAN_APPROVED)
    retrieved_after = temp_db.get_committee_verdict("cand_msft_001")
    assert retrieved_after.hitl_status == HITLStatus.HUMAN_APPROVED


def test_order_fill_and_position_lifecycle(temp_db):
    now = datetime.now(timezone.utc)
    candidate = ScreenedCandidate(
        candidate_id="cand_nvda_001",
        timestamp=now,
        ticker="NVDA",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        risk_r=5.0,
        allocated_usd=30.0,
        rank_score=2.1,
        status=CandidateStatus.APPROVED,
    )
    temp_db.save_candidate(candidate)

    order = Order(
        order_id="ord_nvda_01",
        client_order_id="client_nvda_01",
        candidate_id="cand_nvda_001",
        ticker="NVDA",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        allocated_usd=30.0,
        target_qty=0.3,
        stop_loss=95.0,
        take_profit=110.0,
        state=OrderState.PENDING_RISK_CHECK,
    )
    temp_db.save_order(order)

    # Retrieval
    retrieved_order = temp_db.get_order_by_client_id("client_nvda_01")
    assert retrieved_order is not None
    assert retrieved_order.allocated_usd == 30.0

    # Transition order state
    temp_db.update_order_state("ord_nvda_01", OrderState.FILLED)
    assert temp_db.get_order("ord_nvda_01").state == OrderState.FILLED

    # Add fill
    fill = Fill(
        fill_id="fill_001",
        order_id="ord_nvda_01",
        broker_order_id="broker_ord_123",
        ticker="NVDA",
        side=OrderSide.BUY,
        filled_qty=0.3,
        filled_price=100.0,
        filled_notional=30.0,
        broker_fee_usd=0.0,
        slippage_usd=0.01,
        executed_at=now,
    )
    temp_db.save_fill(fill)
    fills = temp_db.get_fills_for_order("ord_nvda_01")
    assert len(fills) == 1
    assert fills[0].filled_price == 100.0

    # Open Position
    pos = Position(
        position_id="pos_nvda_01",
        ticker="NVDA",
        side=OrderSide.BUY,
        qty=0.3,
        entry_price=100.0,
        current_price=102.5,
        stop_loss=95.0,
        take_profit=110.0,
        market_value=30.75,
        unrealized_pnl=0.75,
        status=PositionStatus.OPEN,
        opened_at=now,
    )
    temp_db.save_position(pos)

    open_positions = temp_db.get_open_positions()
    assert len(open_positions) == 1
    assert open_positions[0].ticker == "NVDA"

    # Close Position
    pos.status = PositionStatus.CLOSED
    pos.closed_at = datetime.now(timezone.utc)
    pos.realized_pnl = 3.0
    pos.exit_reason = ExitReason.TAKE_PROFIT
    temp_db.update_position(pos)

    assert len(temp_db.get_open_positions()) == 0
    closed_pos = temp_db.get_position("pos_nvda_01")
    assert closed_pos.status == PositionStatus.CLOSED
    assert closed_pos.exit_reason == ExitReason.TAKE_PROFIT


def test_portfolio_snapshots_and_audit_logs(temp_db):
    now = datetime.now(timezone.utc)
    snap = PortfolioSnapshot(
        timestamp=now,
        total_equity=101.50,
        cash_balance=72.00,
        invested_capital=29.50,
        unrealized_pnl=0.50,
        active_slots_used=1,
        circuit_breaker_tier=CircuitBreakerTier.NORMAL,
    )
    temp_db.save_portfolio_snapshot(snap)

    latest = temp_db.get_latest_portfolio_snapshot()
    assert latest is not None
    assert latest.total_equity == 101.50
    assert latest.circuit_breaker_tier == CircuitBreakerTier.NORMAL

    # Audit log
    temp_db.save_audit_log(
        severity=AuditSeverity.WARNING,
        component="RiskEngine",
        event_name="CIRCUIT_BREAKER_WARNING",
        message="Equity close to $80 soft threshold",
        metadata={"equity": 82.5},
    )
    logs = temp_db.get_audit_logs(limit=10, severity=AuditSeverity.WARNING)
    assert len(logs) == 1
    assert logs[0].event_name == "CIRCUIT_BREAKER_WARNING"
    assert logs[0].metadata["equity"] == 82.5


def test_deliberation_cache(temp_db):
    entry = DeliberationCacheEntry(
        cache_key="sha256_mock_hash_123",
        symbol="SPY",
        as_of_date="2026-10-01",
        strategy_id="TREND_PULLBACK",
        agent_role="adversarial_risk",
        prompt_hash="prompt_v1",
        model_name="mock-gemini",
        response_json='{"stance": "BULLISH", "score_10": 8.0}',
    )
    temp_db.save_deliberation_cache(entry)

    cached = temp_db.get_deliberation_cache("sha256_mock_hash_123")
    assert cached is not None
    assert cached.symbol == "SPY"
    assert '"score_10": 8.0' in cached.response_json
    assert temp_db.get_deliberation_cache("non_existent_key") is None
