"""
Unit tests for Streamlit Observability Dashboard views and data extraction helpers.
Verifies:
- Run health, data freshness, reconciliation, protection status, and decision lineage views
  derive strictly from persisted records.
- Zero demo/reference fallback values appear as live state.
"""

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.domain.models import (
    AgentRole,
    AgentStance,
    CandidateStatus,
    CircuitBreakerTier,
    CommitteeVerdict,
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
    ProtectionMode,
    ScreenedCandidate,
    StrategyType,
    VerdictOutcome,
)
from src.observability.dashboard import (
    get_data_freshness_data,
    get_decision_lineage_data,
    get_protection_status_data,
    get_reconciliation_data,
    get_run_health_data,
    render_dashboard,
)
from src.storage.db import Database


@pytest.fixture
def dash_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(db_path=path)
    yield db
    if os.path.exists(path):
        os.remove(path)


def test_run_health_empty_and_persisted(dash_db):
    # Empty DB
    assert get_run_health_data(dash_db) is None

    # Persisted run
    dash_db.acquire_run_lease("run_obs_101", trigger="CRON")
    dash_db.release_run_lease("run_obs_101", final_status="COMPLETED")

    run = get_run_health_data(dash_db)
    assert run is not None
    assert run["run_id"] == "run_obs_101"
    assert run["status"] == "COMPLETED"
    assert run["trigger"] == "CRON"


def test_reconciliation_data_empty_and_persisted(dash_db):
    # Empty DB
    assert get_reconciliation_data(dash_db) is None

    # Record event
    dash_db.record_reconciliation_event(
        event_id="recon_obs_202",
        local_snapshot_hash="hash_loc_abc",
        broker_snapshot_hash="hash_brk_def",
        mismatches_json="[]",
        resolution_status="HEALTHY_MATCH",
    )

    recon = get_reconciliation_data(dash_db)
    assert recon is not None
    assert recon["event_id"] == "recon_obs_202"
    assert recon["resolution_status"] == "HEALTHY_MATCH"
    assert recon["local_snapshot_hash"] == "hash_loc_abc"
    assert recon["broker_snapshot_hash"] == "hash_brk_def"


def test_protection_status_data_empty_and_persisted(dash_db):
    # Empty DB
    assert get_protection_status_data(dash_db) == []

    # Record protection status
    dash_db.save_protection_status(
        protection_id="prot_pos_1",
        ticker="AAPL",
        protection_mode="NATIVE_BRACKET",
        stop_loss_order_id="leg_sl_123",
        take_profit_order_id="leg_tp_456",
        watchdog_healthy=1,
    )

    prots = get_protection_status_data(dash_db)
    assert len(prots) == 1
    assert prots[0]["ticker"] == "AAPL"
    assert prots[0]["protection_mode"] == "NATIVE_BRACKET"
    assert prots[0]["stop_loss_order_id"] == "leg_sl_123"
    assert prots[0]["take_profit_order_id"] == "leg_tp_456"
    assert prots[0]["watchdog_healthy"] == 1


def test_data_freshness_empty_and_persisted(dash_db):
    # Empty DB
    assert get_data_freshness_data(dash_db) == []

    now = datetime.now(timezone.utc)
    snap = MarketSnapshot(
        timestamp=now,
        ticker="MSFT",
        open=400.0,
        high=405.0,
        low=398.0,
        close=402.50,
        volume=12000000.0,
        rsi_14=56.2,
        ema_20=399.0,
        sma_50=392.0,
        sma_200=380.0,
        atr_14=4.5,
        spread_bps=2.8,
    )
    dash_db.save_market_snapshot(snap)

    fresh = get_data_freshness_data(dash_db)
    assert len(fresh) == 1
    assert fresh[0]["ticker"] == "MSFT"
    assert fresh[0]["close"] == 402.50
    assert fresh[0]["spread_bps"] == 2.8
    assert isinstance(fresh[0]["age_seconds"], float)


def test_decision_lineage_data_empty_and_persisted(dash_db):
    # Empty DB
    assert get_decision_lineage_data(dash_db) == []

    now = datetime.now(timezone.utc)
    # 1. Candidate
    cand = ScreenedCandidate(
        candidate_id="cand_lin_1",
        timestamp=now,
        ticker="GOOGL",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=180.0,
        stop_loss=174.0,
        take_profit=192.0,
        risk_r=6.0,
        allocated_usd=28.50,
        rank_score=1.85,
        status=CandidateStatus.APPROVED,
    )
    dash_db.save_candidate(cand)

    # 2. Deliberations
    delib = LLMDeliberation(
        deliberation_id="del_lin_1",
        candidate_id="cand_lin_1",
        ticker="GOOGL",
        agent_role=AgentRole.SENTIMENT_CATALYST,
        model_name="gemini",
        stance=AgentStance.BULLISH,
        score_10=8.5,
        bullish_catalysts=["Cloud AI acceleration"],
        risk_factors=[],
        rationale_summary="Strong revenue beats expected",
    )
    dash_db.save_llm_deliberation(delib)

    # 3. Verdict
    verd = CommitteeVerdict(
        verdict_id="verd_lin_1",
        candidate_id="cand_lin_1",
        ticker="GOOGL",
        timestamp=now,
        composite_score=78.5,
        verdict_outcome=VerdictOutcome.AUTO_APPROVED,
        risk_officer_dissent=False,
    )
    dash_db.save_committee_verdict(verd)

    # 4. Order
    order = Order(
        order_id="ord_lin_1",
        client_order_id="coid_lin_1",
        candidate_id="cand_lin_1",
        ticker="GOOGL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        allocated_usd=28.50,
        target_qty=0.1583,
        stop_loss=174.0,
        take_profit=192.0,
        state=OrderState.ROUTED_TO_BROKER,
    )
    dash_db.save_order(order)

    lineages = get_decision_lineage_data(dash_db)
    assert len(lineages) == 1
    l = lineages[0]
    assert l["candidate_id"] == "cand_lin_1"
    assert l["ticker"] == "GOOGL"
    assert len(l["deliberations"]) == 1
    assert l["deliberations"][0]["role"] == AgentRole.SENTIMENT_CATALYST.value
    assert l["verdict"]["verdict_outcome"] == VerdictOutcome.AUTO_APPROVED.value
    assert l["order"]["order_id"] == "ord_lin_1"


def test_render_dashboard_empty_db_displays_no_demo_values(dash_db, monkeypatch):
    monkeypatch.setenv("DB_PATH", dash_db.db_path)

    import src.observability.dashboard as dash

    mock_st = MagicMock()
    mock_st.columns.side_effect = lambda spec: [
        MagicMock() for _ in (range(spec) if isinstance(spec, int) else range(len(spec)))
    ]
    mock_st.tabs.side_effect = lambda tabs: [MagicMock() for _ in tabs]
    mock_st.expander.return_value.__enter__.return_value = MagicMock()
    mock_st.button.return_value = False

    monkeypatch.setattr(dash, "st", mock_st)

    dash.render_dashboard()

    # Verify no hardcoded demo fallback was displayed as live state
    info_calls = [c[0][0] for c in mock_st.info.call_args_list if c[0]]
    # Must report empty database states
    assert any("No active positions" in str(msg) for msg in info_calls)
    assert any("Insufficient historical snapshots" in str(msg) for msg in info_calls)
    assert any("No candidate deliberations" in str(msg) for msg in info_calls)
    assert any("No trading runs" in str(msg) for msg in info_calls)
    assert any("No protection status" in str(msg) for msg in info_calls)
    assert any("No market data" in str(msg) for msg in info_calls)
    assert any("No broker reconciliation" in str(msg) for msg in info_calls)
