"""
Unit tests for Observability: Streamlit Dashboard data loading and Telegram Bot Daemon.
Tests command handlers, HITL escalation card formatting, and callback execution.
"""

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import pytest

pytestmark = pytest.mark.integration

from src.broker.simulated import SimulatedPaperBroker
from src.domain.models import (
    CandidateStatus,
    CommitteeVerdict,
    HITLStatus,
    LLMDeliberation,
    OrderState,
    Position,
    PositionStatus,
    ScreenedCandidate,
    StrategyType,
    VerdictOutcome,
)
from src.observability.telegram_bot import TelegramBotHandler
from src.risk.engine import RiskEngine
from src.storage.db import Database


@pytest.fixture
def obs_env():
    fd_db, path_db = tempfile.mkstemp(suffix=".db")
    os.close(fd_db)
    db = Database(db_path=path_db)

    fd_lock, path_lock = tempfile.mkstemp(suffix=".lock")
    os.close(fd_lock)
    os.remove(path_lock)
    lock_path = Path(path_lock)

    broker = SimulatedPaperBroker(initial_cash=100.0)
    risk_engine = RiskEngine(db=db, broker=broker, lock_file=lock_path)
    db.record_reconciliation_event("evt_clean", "h_local", "h_broker", "[]", "HEALTHY_MATCH")
    handler = TelegramBotHandler(db=db, broker=broker, risk_engine=risk_engine)

    yield handler, broker, db, risk_engine, lock_path

    if os.path.exists(path_db):
        os.remove(path_db)
    if lock_path.exists():
        lock_path.unlink()


def test_telegram_status_command(obs_env):
    handler, broker, db, _, _ = obs_env
    text = handler.handle_status()
    assert "PORTFOLIO STATUS" in text
    assert "$100.00" in text
    assert "Buffer $10.00: ✅ OK" in text
    assert "0 / 3" in text


def test_telegram_positions_command(obs_env):
    handler, broker, db, _, _ = obs_env
    # Empty positions
    assert "None currently open" in handler.handle_positions()

    # Add open position
    now = datetime.now(timezone.utc)
    broker.set_price("NVDA", 128.50)
    from src.domain.models import OrderRequest, OrderSide
    broker.submit_order(
        OrderRequest(
            ticker="NVDA",
            side=OrderSide.BUY,
            dollar_amount=28.50,
            stop_loss=124.0,
            take_profit=137.0,
        )
    )
    pos_text = handler.handle_positions()
    assert "NVDA" in pos_text
    assert "SL: `$124.00`" in pos_text
    assert "TP: `$137.00`" in pos_text


def test_telegram_journal_command(obs_env):
    handler, broker, db, _, _ = obs_env
    now = datetime.now(timezone.utc)

    cand = ScreenedCandidate(
        candidate_id="cand_j_1",
        timestamp=now,
        ticker="AAPL",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=150.0,
        stop_loss=145.0,
        take_profit=160.0,
        risk_r=5.0,
        allocated_usd=28.5,
        rank_score=1.5,
    )
    db.save_candidate(cand)

    from src.domain.models import AgentRole, AgentStance
    delib = LLMDeliberation(
        deliberation_id="d_j_1",
        candidate_id="cand_j_1",
        ticker="AAPL",
        agent_role=AgentRole.SENTIMENT_CATALYST,
        model_name="mock-gemini",
        stance=AgentStance.BULLISH,
        score_10=8.5,
        bullish_catalysts=["Earnings growth"],
        risk_factors=[],
        rationale_summary="Excellent revenue trajectory.",
    )
    db.save_llm_deliberation(delib)

    journal_text = handler.handle_journal("AAPL")
    assert "LLM DELIBERATION JOURNAL: AAPL" in journal_text
    assert "Excellent revenue trajectory." in journal_text


def test_telegram_emergency_and_resume(obs_env):
    handler, broker, db, risk_engine, lock_path = obs_env

    # 1. Emergency liquidate
    msg_liq = handler.handle_emergency_liquidate()
    assert "EMERGENCY LIQUIDATION ENGAGED" in msg_liq
    assert risk_engine.is_hard_locked() is True
    assert lock_path.exists()

    # 2. Resume
    msg_resume = handler.handle_resume()
    assert "TRADING RESUMED" in msg_resume
    assert risk_engine.is_hard_locked() is False
    assert not lock_path.exists()


def test_hitl_escalation_card_and_callback_approval(obs_env):
    handler, broker, db, risk_engine, _ = obs_env
    now = datetime.now(timezone.utc)
    broker.set_price("TSLA", 250.0)

    cand = ScreenedCandidate(
        candidate_id="cand_split_1",
        timestamp=now,
        ticker="TSLA",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=250.0,
        stop_loss=240.0,
        take_profit=270.0,
        risk_r=10.0,
        allocated_usd=28.50,
        rank_score=1.8,
        status=CandidateStatus.HITL_ESCALATED,
    )
    db.save_candidate(cand)

    verdict = CommitteeVerdict(
        verdict_id="v_split_1",
        candidate_id="cand_split_1",
        ticker="TSLA",
        timestamp=now,
        composite_score=68.5,
        verdict_outcome=VerdictOutcome.HITL_ESCALATED,
        risk_officer_dissent=True,
        hitl_status=HITLStatus.PENDING_TELEGRAM_RESPONSE,
    )
    db.save_committee_verdict(verdict)

    from src.domain.models import AgentRole, AgentStance
    delibs = [
        LLMDeliberation(
            deliberation_id="d1",
            candidate_id="cand_split_1",
            ticker="TSLA",
            agent_role=AgentRole.SENTIMENT_CATALYST,
            model_name="m",
            stance=AgentStance.BULLISH,
            score_10=8.5,
            bullish_catalysts=[],
            risk_factors=[],
            rationale_summary="Bullish news sentiment",
        ),
        LLMDeliberation(
            deliberation_id="d2",
            candidate_id="cand_split_1",
            ticker="TSLA",
            agent_role=AgentRole.TECHNICAL_STRUCTURE,
            model_name="m",
            stance=AgentStance.BULLISH,
            score_10=8.0,
            bullish_catalysts=[],
            risk_factors=[],
            rationale_summary="Clean 20 EMA bounce",
        ),
        LLMDeliberation(
            deliberation_id="d3",
            candidate_id="cand_split_1",
            ticker="TSLA",
            agent_role=AgentRole.ADVERSARIAL_RISK,
            model_name="m",
            stance=AgentStance.VETO,
            score_10=3.5,
            bullish_catalysts=[],
            risk_factors=["FOMC risk"],
            rationale_summary="FOMC event risk introduces downside gap danger",
        ),
    ]

    card = handler.format_hitl_escalation_card(cand, verdict, delibs)
    assert "[TRADE DELIBERATION SPLIT: TSLA]" in card["text"]
    assert "HITL DEADLOCK" in card["text"]
    assert len(card["reply_markup"]["inline_keyboard"][0]) == 2

    # Operator approves trade
    ok, resp = handler.handle_callback(f"hitl_approve:{cand.candidate_id}")
    assert ok is True
    assert "Trade APPROVED by Operator" in resp

    # Check status updated in db
    cand_after = db.get_candidate("cand_split_1")
    assert cand_after.status == CandidateStatus.APPROVED
    verd_after = db.get_committee_verdict("cand_split_1")
    assert verd_after.hitl_status == HITLStatus.HUMAN_APPROVED

    # Check broker has position
    assert len(broker.get_positions()) == 1
    assert broker.get_positions()[0].ticker == "TSLA"


def test_dashboard_render_with_data(obs_env, monkeypatch):
    from unittest.mock import MagicMock
    import src.observability.dashboard as dash

    handler, broker, db, _, _ = obs_env
    monkeypatch.setenv("DB_PATH", db.db_path)

    # Mock streamlit components
    mock_st = MagicMock()
    mock_st.columns.side_effect = lambda spec: [
        MagicMock() for _ in (range(spec) if isinstance(spec, int) else range(len(spec)))
    ]
    mock_st.expander.return_value.__enter__.return_value = MagicMock()
    mock_st.button.return_value = False

    monkeypatch.setattr(dash, "st", mock_st)

    # Should execute without throwing
    dash.render_dashboard()
    assert mock_st.title.called
    assert mock_st.metric.called

