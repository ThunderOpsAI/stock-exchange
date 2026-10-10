"""
Unit tests for Triad LLM Deliberation Desk & Consensus Engine.
Tests scoring, auto-approval (>= 74.5%), deadlock HITL escalation, and SQLite SHA-256 caching.
"""

import os
import tempfile
from datetime import datetime, timezone
import pytest

pytestmark = pytest.mark.unit

from src.domain.models import (
    CandidateStatus,
    HITLStatus,
    ScreenedCandidate,
    StrategyType,
    VerdictOutcome,
)
from src.llm.committee import CacheMode, TriadLLMCommittee
from src.storage.db import Database


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(db_path=path)
    yield db
    if os.path.exists(path):
        os.remove(path)


def test_auto_approval_consensus(temp_db):
    now = datetime.now(timezone.utc)
    candidate = ScreenedCandidate(
        candidate_id="cand_aapl_100",
        timestamp=now,
        ticker="AAPL",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=150.0,
        stop_loss=145.0,
        take_profit=160.0,
        risk_r=5.0,
        allocated_usd=28.50,
        rank_score=1.95,
        status=CandidateStatus.PENDING_DELIBERATION,
    )
    temp_db.save_candidate(candidate)

    temp_db.save_candidate(candidate)
    committee = TriadLLMCommittee(db=temp_db, cache_mode=CacheMode.RECORD_ON_MISS)
    headlines = [{"title": "AAPL beats earnings and expands cloud services"}]

    verdict, delibs = committee.deliberate(candidate, headlines=headlines)

    assert len(delibs) == 3
    assert verdict.composite_score >= 74.5
    assert verdict.verdict_outcome == VerdictOutcome.AUTO_APPROVED
    assert verdict.risk_officer_dissent is False

    # Check persistence
    stored_candidate = temp_db.get_candidate("cand_aapl_100")
    assert stored_candidate.status == CandidateStatus.APPROVED


def test_deadlock_hitl_escalation(temp_db):
    now = datetime.now(timezone.utc)
    candidate = ScreenedCandidate(
        candidate_id="cand_tsla_200",
        timestamp=now,
        ticker="TSLA",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=250.0,
        stop_loss=240.0,
        take_profit=270.0,
        risk_r=10.0,
        allocated_usd=29.00,
        rank_score=1.50,
        status=CandidateStatus.PENDING_DELIBERATION,
    )
    temp_db.save_candidate(candidate)

    temp_db.save_candidate(candidate)
    committee = TriadLLMCommittee(db=temp_db, cache_mode=CacheMode.RECORD_ON_MISS)
    # Force risk dissent to trigger the 51% Bullish vs 49% Risk Dissent deadlock
    verdict, delibs = committee.deliberate(candidate, force_risk_dissent=True)

    assert verdict.risk_officer_dissent is True
    assert verdict.verdict_outcome == VerdictOutcome.HITL_ESCALATED
    assert verdict.hitl_status == HITLStatus.PENDING_TELEGRAM_RESPONSE

    stored_candidate = temp_db.get_candidate("cand_tsla_200")
    assert stored_candidate.status == CandidateStatus.HITL_ESCALATED


def test_auto_drop_on_negative_headlines(temp_db):
    now = datetime.now(timezone.utc)
    candidate = ScreenedCandidate(
        candidate_id="cand_bad_300",
        timestamp=now,
        ticker="BAD",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=50.0,
        stop_loss=40.0,
        take_profit=65.0,
        risk_r=10.0,
        allocated_usd=30.00,
        rank_score=0.20,
        status=CandidateStatus.PENDING_DELIBERATION,
    )
    temp_db.save_candidate(candidate)

    temp_db.save_candidate(candidate)
    committee = TriadLLMCommittee(db=temp_db, cache_mode=CacheMode.RECORD_ON_MISS)
    bad_headlines = [
        {"title": "BAD faces SEC investigation and severe earnings miss"},
        {"title": "Analyst downgrade to sell as margins slump"},
    ]

    verdict, delibs = committee.deliberate(
        candidate, headlines=bad_headlines, force_risk_dissent=True
    )

    assert verdict.verdict_outcome == VerdictOutcome.AUTO_DROPPED
    stored_candidate = temp_db.get_candidate("cand_bad_300")
    assert stored_candidate.status == CandidateStatus.VETOED


def test_sha256_deliberation_caching_replay_strict(temp_db):
    now = datetime.now(timezone.utc)
    candidate = ScreenedCandidate(
        candidate_id="cand_cached_400",
        timestamp=now,
        ticker="SPY",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=500.0,
        stop_loss=490.0,
        take_profit=520.0,
        risk_r=10.0,
        allocated_usd=30.00,
        rank_score=2.0,
        status=CandidateStatus.PENDING_DELIBERATION,
    )
    temp_db.save_candidate(candidate)

    # 1. Record on miss first
    comm_record = TriadLLMCommittee(db=temp_db, cache_mode=CacheMode.RECORD_ON_MISS)
    verdict1, _ = comm_record.deliberate(candidate)

    # 2. Replay strict on the same candidate should hit cache seamlessly
    comm_replay = TriadLLMCommittee(db=temp_db, cache_mode=CacheMode.REPLAY_STRICT)
    verdict2, _ = comm_replay.deliberate(candidate)

    assert verdict1.composite_score == verdict2.composite_score
    assert verdict1.verdict_outcome == verdict2.verdict_outcome

    # 3. New unseen candidate under REPLAY_STRICT should raise KeyError on cache miss
    unseen = ScreenedCandidate(
        candidate_id="cand_unseen_500",
        timestamp=now,
        ticker="NEW",
        strategy=StrategyType.MEAN_REVERSION,
        entry_est=100.0,
        stop_loss=90.0,
        take_profit=115.0,
        risk_r=10.0,
        allocated_usd=25.00,
        rank_score=1.0,
        status=CandidateStatus.PENDING_DELIBERATION,
    )
    with pytest.raises(KeyError) as exc_info:
        comm_replay.deliberate(unseen)
    assert "REPLAY_STRICT Cache Miss" in str(exc_info.value)

def test_committee_veto_hitl_escalation(temp_db):
    now = datetime.now(timezone.utc)
    candidate = ScreenedCandidate(
        candidate_id="cand_tsla_201",
        timestamp=now,
        ticker="TSLA",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=250.0,
        stop_loss=240.0,
        take_profit=270.0,
        risk_r=10.0,
        allocated_usd=29.00,
        rank_score=1.50,
        status=CandidateStatus.PENDING_DELIBERATION,
    )
    temp_db.save_candidate(candidate)

    temp_db.save_candidate(candidate)
    committee = TriadLLMCommittee(db=temp_db, cache_mode=CacheMode.RECORD_ON_MISS)
    
    from src.llm.agents import AgentDeliberationOutput, AgentStance
    
    mock = __import__("unittest.mock").mock.patch.object(committee, "_evaluate_agent_with_cache", side_effect=[
        AgentDeliberationOutput(agent_role="sentiment_catalyst", model_name="test", stance=AgentStance.BULLISH, score_10=8, bullish_catalysts=[], risk_factors=[], rationale_summary=""),
        AgentDeliberationOutput(agent_role="technical_structure", model_name="test", stance=AgentStance.BULLISH, score_10=8, bullish_catalysts=[], risk_factors=[], rationale_summary=""),
        AgentDeliberationOutput(agent_role="adversarial_risk", model_name="test", stance=AgentStance.VETO, score_10=1, bullish_catalysts=[], risk_factors=[], rationale_summary=""),
    ])
    mock.start()
    verdict, delibs = committee.deliberate(candidate)
    mock.stop()
    
    assert verdict.risk_officer_dissent is True
    assert verdict.verdict_outcome == VerdictOutcome.HITL_ESCALATED
    
def test_committee_grey_zone_and_auto_dropped(temp_db):
    now = datetime.now(timezone.utc)
    candidate = ScreenedCandidate(
        candidate_id="cand_xyz",
        timestamp=now,
        ticker="XYZ",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=250.0,
        stop_loss=240.0,
        take_profit=270.0,
        risk_r=10.0,
        allocated_usd=29.00,
        rank_score=1.50,
        status=CandidateStatus.PENDING_DELIBERATION,
    )
    temp_db.save_candidate(candidate)

    temp_db.save_candidate(candidate)
    committee = TriadLLMCommittee(db=temp_db, cache_mode=CacheMode.RECORD_ON_MISS)
    
    from src.llm.agents import AgentDeliberationOutput, AgentStance
    
    mock = __import__("unittest.mock").mock.patch.object(committee, "_evaluate_agent_with_cache", side_effect=[
        AgentDeliberationOutput(agent_role="sentiment_catalyst", model_name="test", stance=AgentStance.NEUTRAL, score_10=7, bullish_catalysts=[], risk_factors=[], rationale_summary=""),
        AgentDeliberationOutput(agent_role="technical_structure", model_name="test", stance=AgentStance.NEUTRAL, score_10=7, bullish_catalysts=[], risk_factors=[], rationale_summary=""),
        AgentDeliberationOutput(agent_role="adversarial_risk", model_name="test", stance=AgentStance.NEUTRAL, score_10=6.5, bullish_catalysts=[], risk_factors=[], rationale_summary=""),
    ])
    mock.start()
    verdict, delibs = committee.deliberate(candidate)
    mock.stop()
    assert verdict.verdict_outcome == VerdictOutcome.HITL_ESCALATED
    
    mock = __import__("unittest.mock").mock.patch.object(committee, "_evaluate_agent_with_cache", side_effect=[
        AgentDeliberationOutput(agent_role="sentiment_catalyst", model_name="test", stance=AgentStance.NEUTRAL, score_10=5, bullish_catalysts=[], risk_factors=[], rationale_summary=""),
        AgentDeliberationOutput(agent_role="technical_structure", model_name="test", stance=AgentStance.NEUTRAL, score_10=5, bullish_catalysts=[], risk_factors=[], rationale_summary=""),
        AgentDeliberationOutput(agent_role="adversarial_risk", model_name="test", stance=AgentStance.NEUTRAL, score_10=5, bullish_catalysts=[], risk_factors=[], rationale_summary=""),
    ])
    mock.start()
    verdict2, delibs2 = committee.deliberate(candidate)
    mock.stop()
    assert verdict2.verdict_outcome == VerdictOutcome.AUTO_DROPPED
    
def test_committee_enriched_digest_incomplete(temp_db):
    now = datetime.now(timezone.utc)
    candidate = ScreenedCandidate(
        candidate_id="cand_inc",
        timestamp=now,
        ticker="INC",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=250.0,
        stop_loss=240.0,
        take_profit=270.0,
        risk_r=10.0,
        allocated_usd=29.00,
        rank_score=1.50,
        status=CandidateStatus.PENDING_DELIBERATION,
    )
    
    temp_db.save_candidate(candidate)
    committee = TriadLLMCommittee(db=temp_db, cache_mode=CacheMode.RECORD_ON_MISS)
    
    class IncompleteDigest:
        is_complete = False
        blocking_reason = "Test missing"
        def to_dict(self): return {}
        
    verdict, delibs = committee.deliberate(candidate, enriched_digest=IncompleteDigest())
    assert verdict.verdict_outcome == VerdictOutcome.AUTO_DROPPED
    
    class CompleteDigest:
        is_complete = True
        def to_dict(self): return {"is_complete": True}
        
    from src.llm.agents import AgentDeliberationOutput, AgentStance
    mock = __import__("unittest.mock").mock.patch.object(committee, "_evaluate_agent_with_cache", return_value=AgentDeliberationOutput(agent_role="sentiment_catalyst", model_name="m", stance=AgentStance.BULLISH, score_10=10, bullish_catalysts=[], risk_factors=[], rationale_summary=""))
    mock.start()
    verdict, delibs = committee.deliberate(candidate, enriched_digest=CompleteDigest())
    mock.stop()
    assert verdict.verdict_outcome == VerdictOutcome.AUTO_APPROVED

