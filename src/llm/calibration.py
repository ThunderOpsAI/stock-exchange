"""
Decision Intelligence Quality, Calibration, Disagreement, and Veto Precision Reporting.
Covers Ticket 31 (P6-03):
- Measures persona calibration, pairwise disagreement, HITL override outcomes, and veto precision.
- Strictly enforces safety invariant: no metric alone changes risk limits automatically.
- Deterministic risk engine remains the final authority.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


@dataclass
class PersonaCalibrationMetrics:
    role: str
    total_evaluations: int
    mean_score: float
    score_pnl_correlation: float
    bullish_accuracy_pct: float


@dataclass
class HITLOverrideOutcome:
    candidate_id: str
    ticker: str
    original_committee_outcome: str
    human_decision: str  # APPROVED or REJECTED
    realized_pnl_usd: Optional[float] = None
    hypothetical_pnl_usd: Optional[float] = None
    was_profitable: bool = False


@dataclass
class CommitteeDecisionQualityReport:
    report_id: str
    generated_at: str
    total_deliberations: int
    auto_approved_count: int
    hitl_escalated_count: int
    auto_dropped_count: int
    disagreement_rate_pct: float
    pairwise_disagreements: Dict[str, float]
    risk_veto_precision_pct: float
    net_veto_preservation_usd: float
    hitl_override_summary: Dict[str, Any]
    persona_metrics: Dict[str, PersonaCalibrationMetrics]
    risk_limits_modified_automatically: bool = False  # Mandatory invariant: MUST be False
    safety_invariant_note: str = (
        "Invariant preserved: Decision quality metrics are observational only; "
        "no metric alone changes risk limits or overrides deterministic risk authority."
    )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["persona_metrics"] = {
            k: asdict(v) if hasattr(v, "__dataclass_fields__") else v
            for k, v in self.persona_metrics.items()
        }
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


class CommitteeCalibrationEngine:
    """
    Analyzes committee deliberation quality, agent stance disagreement,
    human operator override efficacy, and veto precision.
    """

    def __init__(self):
        pass

    def evaluate_quality(
        self,
        deliberation_records: List[Dict[str, Any]],
        hitl_records: Optional[List[Dict[str, Any]]] = None,
        trade_outcomes: Optional[Dict[str, float]] = None,
        counterfactual_outcomes: Optional[Dict[str, float]] = None,
        report_id: Optional[str] = None,
    ) -> CommitteeDecisionQualityReport:
        """
        Computes comprehensive decision quality report.
        """
        now = datetime.now(timezone.utc)
        rep_id = report_id or f"dq_rep_{now.strftime('%Y%m%d_%H%M%S')}"

        trades = trade_outcomes or {}
        counterfactuals = counterfactual_outcomes or {}
        hitl_list = hitl_records or []

        total_delibs = len(deliberation_records)
        auto_approved = 0
        hitl_escalated = 0
        auto_dropped = 0

        # Disagreement counters
        sentiment_risk_conflicts = 0
        tech_risk_conflicts = 0
        sentiment_tech_conflicts = 0

        # Persona score and PnL collections
        persona_scores: Dict[str, List[float]] = {
            "sentiment_catalyst": [],
            "technical_structure": [],
            "adversarial_risk": [],
        }
        persona_pnls: Dict[str, List[float]] = {
            "sentiment_catalyst": [],
            "technical_structure": [],
            "adversarial_risk": [],
        }

        # Veto tracking
        veto_count = 0
        veto_losses_avoided = 0
        net_veto_usd = 0.0

        for rec in deliberation_records:
            cid = rec.get("candidate_id", "")
            outcome = rec.get("verdict_outcome", "")
            if outcome == "AUTO_APPROVED":
                auto_approved += 1
            elif outcome == "HITL_ESCALATED":
                hitl_escalated += 1
            else:
                auto_dropped += 1

            agents = rec.get("agent_deliberations", {})
            sent = agents.get("sentiment_catalyst", {})
            tech = agents.get("technical_structure", {})
            risk = agents.get("adversarial_risk", {})

            sent_stance = sent.get("stance", "NEUTRAL")
            tech_stance = tech.get("stance", "NEUTRAL")
            risk_stance = risk.get("stance", "NEUTRAL")

            # Check pairwise disagreements
            if sent_stance in ("BULLISH", "NEUTRAL") and risk_stance in ("BEARISH", "VETO"):
                sentiment_risk_conflicts += 1
            if tech_stance in ("BULLISH", "NEUTRAL") and risk_stance in ("BEARISH", "VETO"):
                tech_risk_conflicts += 1
            if (sent_stance == "BULLISH" and tech_stance == "BEARISH") or (sent_stance == "BEARISH" and tech_stance == "BULLISH"):
                sentiment_tech_conflicts += 1

            # Check vetoes
            if risk_stance == "VETO" or outcome == "AUTO_DROPPED":
                veto_count += 1
                hyp_pnl = counterfactuals.get(cid, 0.0)
                if hyp_pnl < 0:
                    veto_losses_avoided += 1
                net_veto_usd -= hyp_pnl  # If hyp_pnl was -10, net_veto is +10

            # Collect scores and realized PnLs if traded
            if cid in trades:
                pnl = trades[cid]
                if "score_10" in sent:
                    persona_scores["sentiment_catalyst"].append(sent["score_10"])
                    persona_pnls["sentiment_catalyst"].append(pnl)
                if "score_10" in tech:
                    persona_scores["technical_structure"].append(tech["score_10"])
                    persona_pnls["technical_structure"].append(pnl)
                if "score_10" in risk:
                    persona_scores["adversarial_risk"].append(risk["score_10"])
                    persona_pnls["adversarial_risk"].append(pnl)

        # Disagreement rate
        disagreement_rate = (
            round((hitl_escalated + sentiment_risk_conflicts) / total_delibs * 100.0, 1)
            if total_delibs > 0
            else 0.0
        )
        pairwise = {
            "sentiment_vs_risk_pct": round(sentiment_risk_conflicts / total_delibs * 100.0, 1) if total_delibs else 0.0,
            "technical_vs_risk_pct": round(tech_risk_conflicts / total_delibs * 100.0, 1) if total_delibs else 0.0,
            "sentiment_vs_technical_pct": round(sentiment_tech_conflicts / total_delibs * 100.0, 1) if total_delibs else 0.0,
        }

        # Veto precision
        veto_precision = round(veto_losses_avoided / veto_count * 100.0, 1) if veto_count > 0 else 0.0

        # Persona correlations
        persona_metrics: Dict[str, PersonaCalibrationMetrics] = {}
        for role in ["sentiment_catalyst", "technical_structure", "adversarial_risk"]:
            scs = persona_scores[role]
            pnls = persona_pnls[role]
            corr = 0.0
            if len(scs) >= 2 and np.std(scs) > 0 and np.std(pnls) > 0:
                corr = float(np.corrcoef(scs, pnls)[0, 1])

            mean_sc = round(float(np.mean(scs)), 2) if scs else 0.0
            bull_acc = 0.0
            if scs:
                profitable_trades = sum(1 for p in pnls if p > 0)
                bull_acc = round(profitable_trades / len(pnls) * 100.0, 1)

            persona_metrics[role] = PersonaCalibrationMetrics(
                role=role,
                total_evaluations=len(scs),
                mean_score=mean_sc,
                score_pnl_correlation=round(corr, 3),
                bullish_accuracy_pct=bull_acc,
            )

        # HITL Overrides
        hitl_approved = 0
        hitl_rejected = 0
        hitl_pnl_sum = 0.0
        hitl_wins = 0

        for h in hitl_list:
            decision = h.get("decision", "REJECTED")
            cid = h.get("candidate_id", "")
            if decision == "APPROVED":
                hitl_approved += 1
                pnl = trades.get(cid, h.get("realized_pnl_usd", 0.0))
                hitl_pnl_sum += pnl
                if pnl > 0:
                    hitl_wins += 1
            else:
                hitl_rejected += 1

        hitl_summary = {
            "total_hitl_escalations": len(hitl_list),
            "approved_by_operator": hitl_approved,
            "rejected_by_operator": hitl_rejected,
            "operator_win_rate_pct": round(hitl_wins / hitl_approved * 100.0, 1) if hitl_approved > 0 else 0.0,
            "operator_net_pnl_usd": round(hitl_pnl_sum, 2),
        }

        return CommitteeDecisionQualityReport(
            report_id=rep_id,
            generated_at=now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            total_deliberations=total_delibs,
            auto_approved_count=auto_approved,
            hitl_escalated_count=hitl_escalated,
            auto_dropped_count=auto_dropped,
            disagreement_rate_pct=disagreement_rate,
            pairwise_disagreements=pairwise,
            risk_veto_precision_pct=veto_precision,
            net_veto_preservation_usd=round(net_veto_usd, 2),
            hitl_override_summary=hitl_summary,
            persona_metrics=persona_metrics,
            risk_limits_modified_automatically=False,  # strictly False
        )
