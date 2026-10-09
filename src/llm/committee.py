"""
Triad LLM Deliberation Committee & Consensus Engine.
Implements:
- Compact structured JSON digest input formatting (<1,000 tokens).
- Consensus state machine: Auto-Approved (>= 74.5%), Deadlock HITL Escalation (51% Bullish vs 49% Risk Dissent), and Auto-Drop.
- SQLite WAL SHA-256 deliberation caching with REPLAY_STRICT, RECORD_ON_MISS, and SYNTHETIC_MOCK modes.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

from src.domain.models import (
    AgentDeliberationOutput,
    AgentRole,
    AgentStance,
    CandidateStatus,
    CommitteeVerdict,
    DeliberationCacheEntry,
    HITLStatus,
    LLMDeliberation,
    ScreenedCandidate,
    VerdictOutcome,
)
from src.llm.agents import (
    AdversarialRiskOfficer,
    SentimentCatalystAnalyst,
    TechnicalStructureAnalyst,
)
from src.storage.db import Database


class CacheMode(str, Enum):
    REPLAY_STRICT = "REPLAY_STRICT"
    RECORD_ON_MISS = "RECORD_ON_MISS"
    SYNTHETIC_MOCK = "SYNTHETIC_MOCK"


class TriadLLMCommittee:
    def __init__(
        self,
        db: Optional[Database] = None,
        cache_mode: CacheMode = CacheMode.RECORD_ON_MISS,
        model_name: str = "gemini-2.5-flash",
    ):
        self.db = db
        self.cache_mode = cache_mode
        self.model_name = model_name

        self.sentiment_analyst = SentimentCatalystAnalyst(model_name=model_name)
        self.technical_analyst = TechnicalStructureAnalyst(model_name=model_name)
        self.risk_officer = AdversarialRiskOfficer(model_name=model_name)

    def build_digest(
        self,
        candidate: ScreenedCandidate,
        df: Optional[pd.DataFrame] = None,
        headlines: Optional[List[Dict[str, Any]]] = None,
        force_risk_dissent: bool = False,
    ) -> Dict[str, Any]:
        """Constructs a compact structured JSON digest (<1,000 tokens)."""
        latest_metrics = {}
        if df is not None and not df.empty:
            last = df.iloc[-1]
            latest_metrics = {
                "rsi_14": round(float(last.get("rsi_14", 50.0)), 1),
                "ema_20": round(float(last.get("ema_20", candidate.entry_est)), 2),
                "sma_50": round(float(last.get("sma_50", candidate.entry_est)), 2),
                "sma_200": round(float(last.get("sma_200", candidate.entry_est * 0.9)), 2),
                "atr_14": round(float(last.get("atr_14", candidate.risk_r)), 2),
                "rvol_20": round(float(last.get("rvol_20", 1.25)), 2),
                "rs_spy_63d": round(float(last.get("rs_spy_63d", 1.08)), 2),
                "spread_bps": round(float(last.get("spread_bps", 3.0)), 1),
            }

        digest = {
            "ticker": candidate.ticker,
            "strategy": candidate.strategy.value,
            "as_of_date": candidate.timestamp.strftime("%Y-%m-%d"),
            "entry_est": candidate.entry_est,
            "stop_loss": candidate.stop_loss,
            "take_profit": candidate.take_profit,
            "risk_r": candidate.risk_r,
            "allocated_usd": candidate.allocated_usd,
            "rank_score": candidate.rank_score,
            "news_headlines": (headlines or [])[:5],
            "force_risk_dissent": force_risk_dissent,
            **latest_metrics,
        }
        return digest

    def _compute_cache_key(
        self,
        symbol: str,
        as_of_date: str,
        strategy_id: str,
        quant_features: str,
        news_hash: str,
        agent_role: str,
        prompt_hash: str,
    ) -> str:
        raw_key = (
            f"{symbol}|{as_of_date}|{strategy_id}|{quant_features}|{news_hash}|"
            f"{agent_role}|{prompt_hash}|{self.model_name}"
        )
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def _evaluate_agent_with_cache(
        self,
        agent,
        digest: Dict[str, Any],
        candidate: ScreenedCandidate,
    ) -> AgentDeliberationOutput:
        role_name = agent.role.value
        as_of_date = digest.get("as_of_date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        quant_str = f"{candidate.entry_est}_{candidate.stop_loss}_{candidate.take_profit}_{candidate.risk_r}"
        news_hash = hashlib.sha256(json.dumps(digest.get("news_headlines", [])).encode()).hexdigest()[:12]
        prompt_hash = hashlib.sha256(agent.get_system_prompt().encode()).hexdigest()[:12]

        cache_key = self._compute_cache_key(
            symbol=candidate.ticker,
            as_of_date=as_of_date,
            strategy_id=candidate.strategy.value,
            quant_features=quant_str,
            news_hash=news_hash,
            agent_role=role_name,
            prompt_hash=prompt_hash,
        )

        # Check Cache
        if self.db and self.cache_mode in (CacheMode.REPLAY_STRICT, CacheMode.RECORD_ON_MISS):
            cached_entry = self.db.get_deliberation_cache(cache_key)
            if cached_entry:
                data = json.loads(cached_entry.response_json)
                return AgentDeliberationOutput(**data)

            if self.cache_mode == CacheMode.REPLAY_STRICT:
                raise KeyError(
                    f"REPLAY_STRICT Cache Miss for {candidate.ticker} / {role_name} on {as_of_date}"
                )

        # Evaluate via agent
        output = agent.evaluate(digest)

        # Record into cache
        if self.db and self.cache_mode == CacheMode.RECORD_ON_MISS:
            entry = DeliberationCacheEntry(
                cache_key=cache_key,
                symbol=candidate.ticker,
                as_of_date=as_of_date,
                strategy_id=candidate.strategy.value,
                agent_role=role_name,
                prompt_hash=prompt_hash,
                model_name=self.model_name,
                response_json=output.model_dump_json(),
            )
            self.db.save_deliberation_cache(entry)

        return output

    def deliberate(
        self,
        candidate: ScreenedCandidate,
        df: Optional[pd.DataFrame] = None,
        headlines: Optional[List[Dict[str, Any]]] = None,
        force_risk_dissent: bool = False,
        enriched_digest: Optional[Any] = None,
    ) -> Tuple[CommitteeVerdict, List[LLMDeliberation]]:
        """
        Executes full committee deliberation across the 3 personas and resolves consensus.
        Fails closed if enriched_digest has missing required contexts.
        """
        if enriched_digest is not None and not getattr(enriched_digest, "is_complete", True):
            reason = getattr(
                enriched_digest,
                "blocking_reason",
                "Missing required context (fail closed ADR 0002)",
            )
            verdict = CommitteeVerdict(
                verdict_id=f"verd_blocked_{candidate.ticker.lower()}_{uuid.uuid4().hex[:6]}",
                candidate_id=candidate.candidate_id,
                ticker=candidate.ticker,
                timestamp=datetime.now(timezone.utc),
                composite_score=0.0,
                verdict_outcome=VerdictOutcome.AUTO_DROPPED,
                risk_officer_dissent=True,
                hitl_status=None,
            )
            return verdict, []

        if enriched_digest is not None:
            digest = enriched_digest.to_dict()
        else:
            digest = self.build_digest(
                candidate, df=df, headlines=headlines, force_risk_dissent=force_risk_dissent
            )

        # 1. Evaluate 3 agents
        out_sent = self._evaluate_agent_with_cache(self.sentiment_analyst, digest, candidate)
        out_tech = self._evaluate_agent_with_cache(self.technical_analyst, digest, candidate)
        out_risk = self._evaluate_agent_with_cache(self.risk_officer, digest, candidate)

        # Calculate composite score (out of 100.0%)
        # Sentiment: 25.5%, Tech: 25.5%, Risk: 49.0%
        composite = (
            (out_sent.score_10 * 10.0 * 0.255)
            + (out_tech.score_10 * 10.0 * 0.255)
            + (out_risk.score_10 * 10.0 * 0.490)
        )
        composite = round(composite, 2)

        # Consensus determination
        analysts_bullish = (
            out_sent.stance in (AgentStance.BULLISH, AgentStance.NEUTRAL)
            and out_tech.stance in (AgentStance.BULLISH, AgentStance.NEUTRAL)
        )
        risk_officer_dissent = out_risk.stance in (AgentStance.BEARISH, AgentStance.VETO)

        if risk_officer_dissent:
            if analysts_bullish and out_risk.stance != AgentStance.VETO:
                # 51% Bullish vs 49% Risk Dissent deadlock
                outcome = VerdictOutcome.HITL_ESCALATED
                hitl_status = HITLStatus.PENDING_TELEGRAM_RESPONSE
            elif analysts_bullish and out_risk.stance == AgentStance.VETO:
                outcome = VerdictOutcome.HITL_ESCALATED
                hitl_status = HITLStatus.PENDING_TELEGRAM_RESPONSE
            else:
                outcome = VerdictOutcome.AUTO_DROPPED
                hitl_status = None
        else:
            if composite >= 74.5:
                outcome = VerdictOutcome.AUTO_APPROVED
                hitl_status = None
            elif composite >= 65.0:
                outcome = VerdictOutcome.HITL_ESCALATED
                hitl_status = HITLStatus.PENDING_TELEGRAM_RESPONSE
            else:
                outcome = VerdictOutcome.AUTO_DROPPED
                hitl_status = None

        # Build Domain Records
        verdict_id = f"verd_{candidate.ticker.lower()}_{uuid.uuid4().hex[:6]}"
        verdict = CommitteeVerdict(
            verdict_id=verdict_id,
            candidate_id=candidate.candidate_id,
            ticker=candidate.ticker,
            timestamp=datetime.now(timezone.utc),
            composite_score=composite,
            verdict_outcome=outcome,
            risk_officer_dissent=risk_officer_dissent,
            hitl_status=hitl_status,
        )

        delibs: List[LLMDeliberation] = []
        for out in (out_sent, out_tech, out_risk):
            d = LLMDeliberation(
                deliberation_id=f"delib_{uuid.uuid4().hex[:8]}",
                candidate_id=candidate.candidate_id,
                ticker=candidate.ticker,
                agent_role=out.agent_role,
                model_name=out.model_name,
                stance=out.stance,
                score_10=out.score_10,
                bullish_catalysts=out.bullish_catalysts,
                risk_factors=out.risk_factors,
                rationale_summary=out.rationale_summary,
                token_cost_usd=0.0008,
            )
            delibs.append(d)

        # Persist to database if db provided
        if self.db:
            for d in delibs:
                self.db.save_llm_deliberation(d)
            self.db.save_committee_verdict(verdict)

            # Update candidate status
            if outcome == VerdictOutcome.AUTO_APPROVED:
                self.db.update_candidate_status(candidate.candidate_id, CandidateStatus.APPROVED)
            elif outcome == VerdictOutcome.HITL_ESCALATED:
                self.db.update_candidate_status(
                    candidate.candidate_id, CandidateStatus.HITL_ESCALATED
                )
            else:
                self.db.update_candidate_status(candidate.candidate_id, CandidateStatus.VETOED)

        return verdict, delibs
