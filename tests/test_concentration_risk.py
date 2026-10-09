"""
Tests for Concentration Caps, Correlation Ceilings, and Event Risk Exclusions (P3-04).
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from src.broker.simulated import SimulatedPaperBroker
from src.domain.models import (
    CandidateStatus,
    OrderSide,
    Position,
    PositionStatus,
    ScreenedCandidate,
    StrategyType,
)
from src.risk.concentration import ConcentrationRiskLimits, ConcentrationRiskManager
from src.risk.engine import RiskEngine
from src.storage.db import Database


@pytest.fixture
def concentration_env():
    fd_db, path_db = tempfile.mkstemp(suffix=".db")
    os.close(fd_db)
    db = Database(db_path=path_db)
    db.record_reconciliation_event("evt_clean", "h_local", "h_broker", "[]", "HEALTHY_MATCH")

    fd_lock, path_lock = tempfile.mkstemp(suffix=".lock")
    os.close(fd_lock)
    os.remove(path_lock)
    lock_path = Path(path_lock)

    broker = SimulatedPaperBroker(initial_cash=100.0)
    limits = ConcentrationRiskLimits(
        max_sector_slots=1,
        max_correlation_threshold=0.85,
        earnings_blackout_days=7,
    )
    manager = ConcentrationRiskManager(db=db, broker=broker, limits=limits)
    engine = RiskEngine(
        db=db,
        broker=broker,
        lock_file=lock_path,
        concentration_limits=limits,
    )

    yield manager, engine, broker, db

    if os.path.exists(path_db):
        os.remove(path_db)
    if lock_path.exists():
        lock_path.unlink()


def _save_open_position(broker: SimulatedPaperBroker, db: Database, ticker: str, sector: str):
    now = datetime.now(timezone.utc)
    pos_id = f"pos_{ticker}"
    pos = Position(
        position_id=pos_id,
        ticker=ticker,
        side=OrderSide.BUY,
        qty=1.0,
        entry_price=100.0,
        current_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        market_value=100.0,
        unrealized_pnl=0.0,
        status=PositionStatus.OPEN,
        opened_at=now,
    )
    broker.positions[pos_id] = pos
    db.save_position(pos)
    db.save_instrument_metadata(
        ticker=ticker,
        exchange="NASDAQ",
        universe_version="v1",
        sector=sector,
    )


def test_sector_concentration_cap(concentration_env):
    manager, _, broker, db = concentration_env

    # Open a Technology position (AAPL)
    _save_open_position(broker, db, "AAPL", "Technology")

    # Metadata for new candidate MSFT (also Technology)
    db.save_instrument_metadata("MSFT", "NASDAQ", "v1", sector="Technology")

    # Metadata for new candidate JNJ (Healthcare)
    db.save_instrument_metadata("JNJ", "NYSE", "v1", sector="Healthcare")

    # 1. Candidate in same sector (Technology) must be rejected
    ok, reason, meta = manager.evaluate_concentration("MSFT")
    assert ok is False
    assert "SECTOR_CONCENTRATION_EXCEEDED" in reason
    assert meta["sector"] == "TECHNOLOGY"

    # 2. Candidate in different sector (Healthcare) must be allowed
    ok_jnj, reason_jnj, _ = manager.evaluate_concentration("JNJ")
    assert ok_jnj is True
    assert reason_jnj is None


def test_corporate_action_event_risk_exclusion(concentration_env):
    manager, _, _, db = concentration_env

    # Active corporate action flag
    db.save_instrument_metadata("TSLA", "NASDAQ", "v1", corporate_action_flag=1)

    ok, reason, meta = manager.evaluate_concentration("TSLA")
    assert ok is False
    assert "EVENT_RISK_EXCLUSION" in reason
    assert meta["event"] == "corporate_action"


def test_earnings_blackout_exclusion(concentration_env):
    manager, _, _, db = concentration_env
    now = datetime.now(timezone.utc)

    # 1. Earnings announcement in 3 days (within 7-day blackout) -> blocked
    db.save_instrument_metadata(
        "NVDA",
        "NASDAQ",
        "v1",
        next_earnings_date=now + timedelta(days=3),
    )
    ok, reason, meta = manager.evaluate_concentration("NVDA", as_of=now)
    assert ok is False
    assert "EVENT_RISK_EXCLUSION" in reason
    assert "earnings" in reason.lower()

    # 2. Earnings announcement in 15 days (outside blackout) -> allowed
    db.save_instrument_metadata(
        "AMD",
        "NASDAQ",
        "v1",
        next_earnings_date=now + timedelta(days=15),
    )
    ok_amd, reason_amd, _ = manager.evaluate_concentration("AMD", as_of=now)
    assert ok_amd is True


def test_pairwise_correlation_threshold(concentration_env):
    manager, _, broker, db = concentration_env

    _save_open_position(broker, db, "SPY", "Broad")

    # Generate synthetic returns series
    np.random.seed(42)
    base_returns = pd.Series(np.random.normal(0.001, 0.01, 50))
    # Highly correlated series (corr > 0.90)
    high_corr_returns = base_returns + np.random.normal(0, 0.002, 50)
    # Low correlated series (uncorrelated noise)
    low_corr_returns = pd.Series(np.random.normal(0.001, 0.01, 50))

    open_returns = {"SPY": base_returns}

    # 1. High correlation candidate -> blocked
    ok, reason, meta = manager.evaluate_concentration(
        "QQQ",
        candidate_returns=high_corr_returns,
        open_position_returns=open_returns,
    )
    assert ok is False
    assert "CORRELATION_CONCENTRATION_EXCEEDED" in reason
    assert meta["corr"] >= 0.85

    # 2. Low correlation candidate -> allowed
    ok_low, reason_low, _ = manager.evaluate_concentration(
        "GLD",
        candidate_returns=low_corr_returns,
        open_position_returns=open_returns,
    )
    assert ok_low is True


def test_risk_engine_blocks_order_on_sector_concentration(concentration_env):
    _, engine, broker, db = concentration_env
    now = datetime.now(timezone.utc)

    # Position in Technology
    _save_open_position(broker, db, "AAPL", "Technology")
    db.save_instrument_metadata("MSFT", "NASDAQ", "v1", sector="Technology")

    cand = ScreenedCandidate(
        candidate_id="cand_msft",
        timestamp=now,
        ticker="MSFT",
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
    assert "SECTOR_CONCENTRATION_EXCEEDED" in msg

    # Audit log check
    logs = db.get_audit_logs(component="ConcentrationRisk")
    assert len(logs) > 0
    assert any("SECTOR_CONCENTRATION_EXCEEDED" in (l.message if hasattr(l, "message") else l["message"]) for l in logs)
