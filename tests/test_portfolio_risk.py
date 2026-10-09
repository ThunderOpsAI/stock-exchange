"""
Tests for Portfolio Risk Limits, Consecutive-Loss Pause, and Stop-Risk Cap (P3-03).
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from src.broker.simulated import SimulatedPaperBroker
from src.domain.models import (
    CandidateStatus,
    ExitReason,
    OrderSide,
    Position,
    PositionStatus,
    ScreenedCandidate,
    StrategyType,
)
from src.risk.engine import RiskEngine
from src.risk.portfolio_risk import PortfolioRiskEvaluator, PortfolioRiskLimits
from src.storage.db import Database


@pytest.fixture
def portfolio_risk_env():
    fd_db, path_db = tempfile.mkstemp(suffix=".db")
    os.close(fd_db)
    db = Database(db_path=path_db)
    db.record_reconciliation_event("evt_clean", "h_local", "h_broker", "[]", "HEALTHY_MATCH")

    fd_lock, path_lock = tempfile.mkstemp(suffix=".lock")
    os.close(fd_lock)
    os.remove(path_lock)
    lock_path = Path(path_lock)

    broker = SimulatedPaperBroker(initial_cash=100.0)
    limits = PortfolioRiskLimits(
        daily_loss_limit_usd=3.0,
        weekly_loss_limit_usd=6.0,
        consecutive_loss_limit=3,
        portfolio_stop_risk_cap_usd=6.0,
    )
    evaluator = PortfolioRiskEvaluator(db=db, broker=broker, limits=limits)
    engine = RiskEngine(
        db=db,
        broker=broker,
        lock_file=lock_path,
        portfolio_risk_limits=limits,
    )

    yield evaluator, engine, broker, db

    if os.path.exists(path_db):
        os.remove(path_db)
    if lock_path.exists():
        lock_path.unlink()


def _save_closed_trade(db: Database, ticker: str, realized_pnl: float, closed_at: datetime):
    now = datetime.now(timezone.utc)
    pos = Position(
        position_id=f"pos_{ticker}_{closed_at.timestamp()}",
        ticker=ticker,
        side=OrderSide.BUY,
        qty=1.0,
        entry_price=100.0,
        current_price=100.0 + realized_pnl,
        stop_loss=95.0,
        take_profit=110.0,
        market_value=0.0,
        unrealized_pnl=0.0,
        status=PositionStatus.CLOSED,
        opened_at=closed_at - timedelta(hours=2),
        closed_at=closed_at,
        realized_pnl=realized_pnl,
        exit_reason=ExitReason.STOP_LOSS if realized_pnl < 0 else ExitReason.TAKE_PROFIT,
    )
    db.save_position(pos)


def test_daily_loss_limit_boundaries(portfolio_risk_env):
    evaluator, _, _, db = portfolio_risk_env
    now = datetime.now(timezone.utc)

    # 1. Under limit: $2.90 realized loss (< $3.00)
    _save_closed_trade(db, "AAPL", -2.90, now - timedelta(hours=1))
    ok, reason, meta = evaluator.evaluate_entry(candidate_risk_usd=0.5, as_of=now)
    assert ok is True
    assert reason is None
    assert meta["daily_losses"] == 2.90

    # 2. Exactly at limit: $3.00 realized loss (>= $3.00)
    _save_closed_trade(db, "MSFT", -0.10, now - timedelta(minutes=30))
    ok, reason, meta = evaluator.evaluate_entry(candidate_risk_usd=0.5, as_of=now)
    assert ok is False
    assert "DAILY_LOSS_LIMIT_EXCEEDED" in reason
    assert meta["daily_losses"] == 3.00

    # 3. Past trades from yesterday do not count towards today's daily limit
    yesterday = now - timedelta(days=1)
    _save_closed_trade(db, "GOOGL", -5.00, yesterday)
    assert evaluator.get_daily_realized_losses(now) == 3.00


def test_weekly_loss_limit_boundaries(portfolio_risk_env):
    evaluator, _, _, db = portfolio_risk_env
    now = datetime.now(timezone.utc)

    # 1. Losses spread across different days: $2.50 two days ago, $2.50 three days ago = $5.00 (< $6.00)
    _save_closed_trade(db, "T1", -2.50, now - timedelta(days=2))
    _save_closed_trade(db, "T2", -2.50, now - timedelta(days=3))
    ok, reason, meta = evaluator.evaluate_entry(candidate_risk_usd=0.5, as_of=now)
    assert ok is True
    assert meta["weekly_losses"] == 5.00

    # 2. Add $1.00 loss four days ago -> weekly loss = $6.00 (>= $6.00)
    _save_closed_trade(db, "T3", -1.00, now - timedelta(days=4))
    ok, reason, meta = evaluator.evaluate_entry(candidate_risk_usd=0.5, as_of=now)
    assert ok is False
    assert "WEEKLY_LOSS_LIMIT_EXCEEDED" in reason


def test_consecutive_loss_pause_and_reset(portfolio_risk_env):
    evaluator, _, _, db = portfolio_risk_env
    now = datetime.now(timezone.utc)

    # 2 consecutive losses (< 3 limit)
    _save_closed_trade(db, "L1", -1.00, now - timedelta(hours=3))
    _save_closed_trade(db, "L2", -1.00, now - timedelta(hours=2))
    assert evaluator.get_consecutive_losses() == 2
    ok, reason, _ = evaluator.evaluate_entry(candidate_risk_usd=0.5, as_of=now)
    assert ok is True

    # 3rd consecutive loss -> triggered!
    _save_closed_trade(db, "L3", -0.50, now - timedelta(hours=1))
    assert evaluator.get_consecutive_losses() == 3
    ok, reason, _ = evaluator.evaluate_entry(candidate_risk_usd=0.5, as_of=now)
    assert ok is False
    assert "CONSECUTIVE_LOSS_PAUSE_ACTIVE" in reason

    # Winning trade resets the streak
    _save_closed_trade(db, "W1", +2.00, now - timedelta(minutes=10))
    assert evaluator.get_consecutive_losses() == 0


def test_portfolio_stop_risk_cap_boundaries(portfolio_risk_env):
    evaluator, _, broker, _ = portfolio_risk_env

    # Simulate 2 open positions on broker with stop loss
    # Pos 1: qty=1.0, entry=100.0, stop=98.0 -> risk = $2.00
    pos1 = Position(
        position_id="pos_1",
        ticker="SYM1",
        side=OrderSide.BUY,
        qty=1.0,
        entry_price=100.0,
        current_price=100.0,
        stop_loss=98.0,
        take_profit=110.0,
        market_value=100.0,
        unrealized_pnl=0.0,
        status=PositionStatus.OPEN,
        opened_at=datetime.now(timezone.utc),
    )
    # Pos 2: qty=1.0, entry=50.0, stop=47.0 -> risk = $3.00
    pos2 = Position(
        position_id="pos_2",
        ticker="SYM2",
        side=OrderSide.BUY,
        qty=1.0,
        entry_price=50.0,
        current_price=50.0,
        stop_loss=47.0,
        take_profit=60.0,
        market_value=50.0,
        unrealized_pnl=0.0,
        status=PositionStatus.OPEN,
        opened_at=datetime.now(timezone.utc),
    )
    broker.positions["pos_1"] = pos1
    broker.positions["pos_2"] = pos2

    # Open risk = $2.00 + $3.00 = $5.00
    assert evaluator.get_open_stop_risk() == 5.00

    # 1. Candidate risk $1.00 -> total $6.00 <= cap $6.00 -> allowed
    ok, reason, meta = evaluator.evaluate_entry(candidate_risk_usd=1.00)
    assert ok is True

    # 2. Candidate risk $1.05 -> total $6.05 > cap $6.00 -> blocked
    ok, reason, meta = evaluator.evaluate_entry(candidate_risk_usd=1.05)
    assert ok is False
    assert "PORTFOLIO_STOP_RISK_CAP_EXCEEDED" in reason


def test_strictest_limit_wins_priority(portfolio_risk_env):
    evaluator, _, broker, db = portfolio_risk_env
    now = datetime.now(timezone.utc)

    # Trigger both consecutive losses (3) and daily loss ($3.20)
    _save_closed_trade(db, "A", -1.00, now - timedelta(hours=3))
    _save_closed_trade(db, "B", -1.00, now - timedelta(hours=2))
    _save_closed_trade(db, "C", -1.20, now - timedelta(hours=1))

    ok, reason, _ = evaluator.evaluate_entry(candidate_risk_usd=0.5, as_of=now)
    assert ok is False
    # Daily loss limit is Priority 1, so it must win over consecutive loss pause
    assert "DAILY_LOSS_LIMIT_EXCEEDED" in reason


def test_risk_engine_integration_blocks_order_on_limit_breach(portfolio_risk_env):
    _, engine, broker, db = portfolio_risk_env
    now = datetime.now(timezone.utc)

    # Exceed daily loss limit in DB
    _save_closed_trade(db, "LOSS1", -3.50, now - timedelta(hours=1))

    cand = ScreenedCandidate(
        candidate_id="cand_aapl",
        timestamp=now,
        ticker="AAPL",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        risk_r=5.0,
        allocated_usd=25.0,
        rank_score=1.0,
        status=CandidateStatus.APPROVED,
    )
    db.save_candidate(cand)

    ok, order, res, msg = engine.validate_and_route_order(cand)
    assert ok is False
    assert "DAILY_LOSS_LIMIT_EXCEEDED" in msg

    # Verify audit log was recorded
    logs = db.get_audit_logs(component="PortfolioRisk")
    assert len(logs) > 0
    assert any("DAILY_LOSS_LIMIT_EXCEEDED" in (l.message if hasattr(l, "message") else l["message"]) for l in logs)
