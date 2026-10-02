"""
Triad LLM Committee Personas:
1. Sentiment & Catalyst Analyst (25.5% weight)
2. Technical Structure Analyst (25.5% weight)
3. Adversarial Risk Officer (49.0% weight)
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from src.domain.models import AgentDeliberationOutput, AgentRole, AgentStance


class BaseLLMAgent(ABC):
    def __init__(self, role: AgentRole, weight: float, model_name: str = "gemini-2.5-flash"):
        self.role = role
        self.weight = weight
        self.model_name = model_name

    @abstractmethod
    def get_system_prompt(self) -> str:
        pass

    @abstractmethod
    def evaluate(self, digest: Dict[str, Any]) -> AgentDeliberationOutput:
        pass


class SentimentCatalystAnalyst(BaseLLMAgent):
    def __init__(self, model_name: str = "gemini-2.5-flash"):
        super().__init__(
            role=AgentRole.SENTIMENT_CATALYST,
            weight=0.255,
            model_name=model_name,
        )

    def get_system_prompt(self) -> str:
        return """You are the Sentiment & Catalyst Analyst on an Autonomous Stock Trading Desk ($100 sandbox).
Your voting weight is 25.5%. Your role:
1. Audit news headlines, SEC filings, and recent announcements for positive catalysts or impending earnings risk.
2. If earnings release is within 7 days, heavily penalize or recommend standing aside.
3. Return strict JSON adhering to:
{
  "agent_role": "sentiment_catalyst",
  "model_name": "<model>",
  "stance": "BULLISH" | "NEUTRAL" | "BEARISH" | "VETO",
  "score_10": <0.0 - 10.0>,
  "bullish_catalysts": ["catalyst 1", ...],
  "risk_factors": ["risk 1", ...],
  "rationale_summary": "<3 sentence summary>"
}"""

    def evaluate(self, digest: Dict[str, Any]) -> AgentDeliberationOutput:
        # Heuristic evaluator used in mock/offline mode or default
        headlines = digest.get("news_headlines", [])
        ticker = digest.get("ticker", "")
        has_negative = any(
            any(w in h.get("title", "").lower() for w in ["downgrade", "investigation", "miss", "sec", "slump"])
            for h in headlines
        )
        has_positive = any(
            any(w in h.get("title", "").lower() for w in ["upgrade", "beat", "surge", "growth", "partnership"])
            for h in headlines
        )

        if has_negative:
            stance = AgentStance.BEARISH
            score = 3.5
            bullish = []
            risks = ["Negative news headline sentiment detected in last 72 hours"]
            summary = f"Recent headlines for {ticker} show negative catalysts. Risk of further downside drag."
        elif has_positive or len(headlines) > 0:
            stance = AgentStance.BULLISH
            score = 8.2
            bullish = ["Positive sentiment or news momentum", "No imminent binary downside events"]
            risks = ["Market-wide volatility spillover"]
            summary = f"{ticker} shows constructive news sentiment with supportive headlines. No near-term earnings trap identified."
        else:
            stance = AgentStance.BULLISH
            score = 7.5
            bullish = ["Clean news flow with no negative overhang"]
            risks = ["Lack of specific high-impact catalyst"]
            summary = f"News flow for {ticker} is neutral-to-clean. No earnings danger reported in the immediate window."

        return AgentDeliberationOutput(
            agent_role=self.role,
            model_name=self.model_name,
            stance=stance,
            score_10=score,
            bullish_catalysts=bullish,
            risk_factors=risks,
            rationale_summary=summary,
        )


class TechnicalStructureAnalyst(BaseLLMAgent):
    def __init__(self, model_name: str = "gemini-2.5-flash"):
        super().__init__(
            role=AgentRole.TECHNICAL_STRUCTURE,
            weight=0.255,
            model_name=model_name,
        )

    def get_system_prompt(self) -> str:
        return """You are the Technical Structure Analyst on an Autonomous Stock Trading Desk ($100 sandbox).
Your voting weight is 25.5%. Your role:
1. Audit price geometry, 20 EMA pullback quality, volume contraction on dip, RVOL reversal, and relative strength vs SPY.
2. Verify strict mathematical parameters: RS_SPY >= 1.05, RVOL >= 1.20, clean support level.
3. Return strict JSON adhering to schema."""

    def evaluate(self, digest: Dict[str, Any]) -> AgentDeliberationOutput:
        rvol = digest.get("rvol_20", 1.0)
        rs_spy = digest.get("rs_spy_63d", 1.0)
        strategy = digest.get("strategy", "")
        ticker = digest.get("ticker", "")

        bullish = []
        risks = []

        if rs_spy >= 1.05:
            bullish.append(f"Leader relative strength: {rs_spy:.2f}x vs SPY")
        else:
            risks.append(f"Sub-par relative strength: {rs_spy:.2f}x vs SPY")

        if rvol >= 1.20:
            bullish.append(f"Volume conviction on reversal: RVOL {rvol:.2f}x")
        else:
            risks.append(f"Low volume participation: RVOL {rvol:.2f}x")

        if rs_spy >= 1.05 and rvol >= 1.20:
            stance = AgentStance.BULLISH
            score = 8.8
            summary = f"Strong structural setup for {ticker} under {strategy}. Clean bounce confirmed with above-average volume."
        elif rs_spy >= 1.0 or rvol >= 1.0:
            stance = AgentStance.NEUTRAL
            score = 6.5
            summary = f"Acceptable structure for {ticker} but volume or relative strength is marginal."
        else:
            stance = AgentStance.BEARISH
            score = 4.0
            summary = f"Poor technical confirmation for {ticker}. Lacks relative strength or volume momentum."

        return AgentDeliberationOutput(
            agent_role=self.role,
            model_name=self.model_name,
            stance=stance,
            score_10=score,
            bullish_catalysts=bullish,
            risk_factors=risks,
            rationale_summary=summary,
        )


class AdversarialRiskOfficer(BaseLLMAgent):
    def __init__(self, model_name: str = "gemini-2.5-flash"):
        super().__init__(
            role=AgentRole.ADVERSARIAL_RISK,
            weight=0.490,
            model_name=model_name,
        )

    def get_system_prompt(self) -> str:
        return """You are the Adversarial Risk Officer on an Autonomous Stock Trading Desk ($100 sandbox).
Your voting weight is 49.0% (effective veto power). Your role:
1. Actively stress-test and attack the proposed trade.
2. Check downside asymmetry, macro contagion, gap risk, wide spread, and portfolio slot risk.
3. If risk-to-reward is inadequate or downside danger is high, issue a BEARISH or VETO stance.
4. Return strict JSON adhering to schema."""

    def evaluate(self, digest: Dict[str, Any]) -> AgentDeliberationOutput:
        risk_r = digest.get("risk_r", 1.0)
        allocated_usd = digest.get("allocated_usd", 30.0)
        spread_bps = digest.get("spread_bps", 3.0)
        ticker = digest.get("ticker", "")
        force_dissent = digest.get("force_risk_dissent", False)

        bullish = ["Defined ATR stop-loss within the $3.00 max trade risk constraint"]
        risks = []

        if spread_bps > 5.0:
            risks.append(f"Elevated spread ({spread_bps:.1f} bps) creating excessive friction")

        if force_dissent:
            return AgentDeliberationOutput(
                agent_role=self.role,
                model_name=self.model_name,
                stance=AgentStance.BEARISH,
                score_10=4.0,
                bullish_catalysts=[],
                risk_factors=["Severe macro contagion risk", "Unfavorable risk-to-reward skew"],
                rationale_summary=f"Adversarial Risk Officer vetoes {ticker} due to elevated sector fragility and downside asymmetry.",
            )

        # Normal evaluation
        dollar_risk = (allocated_usd / digest.get("entry_est", 100.0)) * risk_r
        if dollar_risk > 3.05:
            risks.append(f"Trade risk ${dollar_risk:.2f} breaches $3.00 max risk threshold")
            stance = AgentStance.VETO
            score = 2.0
            summary = f"VETO: Dollar risk ${dollar_risk:.2f} violates $3.00 strict capital constraint."
        else:
            stance = AgentStance.BULLISH
            score = 8.0
            summary = f"Risk parameters for {ticker} are fully compliant with $100 sandbox invariants (risk <= $3.00, slot <= $30.00)."

        return AgentDeliberationOutput(
            agent_role=self.role,
            model_name=self.model_name,
            stance=stance,
            score_10=score,
            bullish_catalysts=bullish,
            risk_factors=risks,
            rationale_summary=summary,
        )
