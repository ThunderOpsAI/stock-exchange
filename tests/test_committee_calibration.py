"""
Unit and integration tests for decision quality, calibration, disagreement, and veto precision.
Covers Ticket 31 (P6-03):
- Dashboard/report shows decision quality.
- No metric alone changes risk limits automatically (deterministic risk engine remains final authority).
"""

import json
import pytest

from src.llm.calibration import (
    CommitteeCalibrationEngine,
    CommitteeDecisionQualityReport,
    PersonaCalibrationMetrics,
)


def test_committee_decision_quality_metrics():
    engine = CommitteeCalibrationEngine()

    deliberations = [
        # Candidate 1: Consensus Bullish -> Traded -> Profitable (+$15)
        {
            "candidate_id": "C1",
            "verdict_outcome": "AUTO_APPROVED",
            "agent_deliberations": {
                "sentiment_catalyst": {"stance": "BULLISH", "score_10": 8.5},
                "technical_structure": {"stance": "BULLISH", "score_10": 9.0},
                "adversarial_risk": {"stance": "BULLISH", "score_10": 7.5},
            },
        },
        # Candidate 2: Deadlock -> Sent to HITL (Disagreement: Bullish vs Bearish Risk)
        {
            "candidate_id": "C2",
            "verdict_outcome": "HITL_ESCALATED",
            "agent_deliberations": {
                "sentiment_catalyst": {"stance": "BULLISH", "score_10": 8.0},
                "technical_structure": {"stance": "BULLISH", "score_10": 7.5},
                "adversarial_risk": {"stance": "BEARISH", "score_10": 4.0},
            },
        },
        # Candidate 3: Risk Officer Vetoed -> Dropped -> Tanked (-$20 counterfactual loss avoided)
        {
            "candidate_id": "C3",
            "verdict_outcome": "AUTO_DROPPED",
            "agent_deliberations": {
                "sentiment_catalyst": {"stance": "BULLISH", "score_10": 7.5},
                "technical_structure": {"stance": "NEUTRAL", "score_10": 6.0},
                "adversarial_risk": {"stance": "VETO", "score_10": 1.0},
            },
        },
        # Candidate 4: Auto approved -> Traded -> Small loss (-$5)
        {
            "candidate_id": "C4",
            "verdict_outcome": "AUTO_APPROVED",
            "agent_deliberations": {
                "sentiment_catalyst": {"stance": "BULLISH", "score_10": 8.0},
                "technical_structure": {"stance": "BULLISH", "score_10": 8.0},
                "adversarial_risk": {"stance": "BULLISH", "score_10": 7.0},
            },
        },
    ]

    trades = {
        "C1": 15.0,
        "C4": -5.0,
    }

    counterfactuals = {
        "C3": -20.0,  # Tanked $20, so VETO avoided a $20 loss!
    }

    hitl_records = [
        {
            "candidate_id": "C2",
            "decision": "APPROVED",
            "realized_pnl_usd": 10.0,
        }
    ]

    report = engine.evaluate_quality(
        deliberation_records=deliberations,
        hitl_records=hitl_records,
        trade_outcomes=trades,
        counterfactual_outcomes=counterfactuals,
        report_id="dq_test_001",
    )

    assert isinstance(report, CommitteeDecisionQualityReport)
    assert report.total_deliberations == 4
    assert report.auto_approved_count == 2
    assert report.hitl_escalated_count == 1
    assert report.auto_dropped_count == 1

    # Disagreement: C2 had sentiment bullish vs risk bearish; C3 had sentiment bullish vs risk veto
    assert report.pairwise_disagreements["sentiment_vs_risk_pct"] == 50.0  # 2 of 4

    # Risk Veto Precision: 1 veto (C3), avoided loss (-$20) -> 100% precision, +$20 preservation
    assert report.risk_veto_precision_pct == 100.0
    assert report.net_veto_preservation_usd == 20.0

    # Persona Metrics
    assert "sentiment_catalyst" in report.persona_metrics
    assert "technical_structure" in report.persona_metrics
    assert "adversarial_risk" in report.persona_metrics

    # HITL Override Summary
    assert report.hitl_override_summary["total_hitl_escalations"] == 1
    assert report.hitl_override_summary["approved_by_operator"] == 1
    assert report.hitl_override_summary["operator_win_rate_pct"] == 100.0
    assert report.hitl_override_summary["operator_net_pnl_usd"] == 10.0

    # Safety Invariant: Risk limits MUST NOT be modified automatically
    assert report.risk_limits_modified_automatically is False
    assert "Invariant preserved" in report.safety_invariant_note

    # Serialization
    j_str = report.to_json()
    parsed = json.loads(j_str)
    assert parsed["report_id"] == "dq_test_001"
    assert parsed["risk_limits_modified_automatically"] is False
