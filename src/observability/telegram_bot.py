"""
Telegram Observability Bot & HITL Escalation Daemon.
Implements:
- Core commands: /status, /positions, /journal [ticker], /soft_freeze, /emergency_liquidate, /resume
- Interactive HITL Escalation Card formatting with inline action buttons
- Callback button handling for human trade approval/rejection
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.broker.base import AbstractBrokerAdapter
from src.domain.models import (
    AuditSeverity,
    CandidateStatus,
    CircuitBreakerTier,
    CommitteeVerdict,
    HITLStatus,
    LLMDeliberation,
    PositionStatus,
    ScreenedCandidate,
    VerdictOutcome,
)
from src.risk.engine import LOCK_FILE_PATH, RiskEngine
from src.storage.db import Database


class TelegramBotHandler:
    def __init__(
        self,
        db: Database,
        broker: AbstractBrokerAdapter,
        risk_engine: RiskEngine,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ):
        self.db = db
        self.broker = broker
        self.risk_engine = risk_engine
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")

    # -------------------------------------------------------------
    # 1. Command Handlers
    # -------------------------------------------------------------
    def handle_status(self) -> str:
        bal = self.broker.get_account_balance()
        positions = self.broker.get_positions()
        tier = self.risk_engine.evaluate_circuit_breaker(bal.equity)
        is_locked = self.risk_engine.is_hard_locked()

        tier_str = "NORMAL (Tier 0)"
        if is_locked:
            tier_str = "HARD EMERGENCY LOCK (Tier 2 - HALTED.lock active)"
        elif tier == CircuitBreakerTier.SOFT_HALT:
            tier_str = "SOFT FREEZE (Tier 1 - Buys Halted)"

        pnl = bal.equity - 100.0
        return (
            f"📊 *PORTFOLIO STATUS* ($100 Sandbox)\n"
            f"• Total Equity: `${bal.equity:.2f}` ({pnl:+.2f} / {pnl:+.1f}%)\n"
            f"• Cash Balance: `${bal.cash:.2f}` (Buffer $10.00: {'✅ OK' if bal.cash >= 10.0 else '⚠️ DEFICIT'})\n"
            f"• Active Slots: `{len(positions)} / 3` ({3 - len(positions)} available)\n"
            f"• Circuit Breaker: `{tier_str}`\n"
            f"• Max Risk Cap: `$3.00 / trade` (3.0% hard cap)"
        )

    def handle_positions(self) -> str:
        positions = self.broker.get_positions()
        if not positions:
            return "💼 *ACTIVE POSITIONS*: None currently open."

        lines = ["💼 *ACTIVE POSITIONS*:"]
        for p in positions:
            unrealized_pct = (
                ((p.current_price - p.entry_price) / p.entry_price) * 100.0
                if p.entry_price > 0
                else 0.0
            )
            lines.append(
                f"• *{p.ticker}*: {p.qty:.4f} shares @ `${p.entry_price:.2f}`\n"
                f"  Current: `${p.current_price:.2f}` | PnL: `${p.unrealized_pnl:+.2f}` ({unrealized_pct:+.1f}%)\n"
                f"  SL: `${p.stop_loss:.2f}` | TP: `${p.take_profit:.2f}`"
            )
        return "\n".join(lines)

    def handle_journal(self, ticker: Optional[str] = None) -> str:
        with self.db.session() as conn:
            if ticker:
                row = conn.execute(
                    "SELECT candidate_id, ticker, strategy FROM screened_candidates WHERE ticker = ? ORDER BY timestamp DESC LIMIT 1",
                    (ticker.upper(),),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT candidate_id, ticker, strategy FROM screened_candidates ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()

        if not row:
            return "📝 *THOUGHT JOURNAL*: No deliberation entries found."

        cand_id = row["candidate_id"]
        sym = row["ticker"]
        strat = row["strategy"]
        delibs = self.db.get_deliberations_for_candidate(cand_id)
        verdict = self.db.get_committee_verdict(cand_id)

        lines = [
            f"📝 *LLM DELIBERATION JOURNAL: {sym}*",
            f"Strategy: `{strat}` | Candidate: `{cand_id}`",
        ]
        if verdict:
            lines.append(
                f"Verdict: `{verdict.verdict_outcome.value}` ({verdict.composite_score:.1f}%) | "
                f"Risk Dissent: `{'YES' if verdict.risk_officer_dissent else 'NO'}`\n"
            )

        for d in delibs:
            role = d.agent_role.value.replace("_", " ").title()
            weight = "49.0%" if "risk" in d.agent_role.value else "25.5%"
            lines.append(
                f"*{role}* ({weight}): `{d.stance.value}` ({d.score_10:.1f}/10)\n"
                f"_{d.rationale_summary}_"
            )

        return "\n\n".join(lines)

    def handle_soft_freeze(self) -> str:
        self.db.save_audit_log(
            severity=AuditSeverity.WARNING,
            component="TelegramBot",
            event_name="OPERATOR_SOFT_FREEZE",
            message="Operator initiated soft freeze via Telegram command.",
        )
        return "⚠️ *SOFT FREEZE ENGAGED*: New buy orders halted. Active positions will run to bracket stops."

    def handle_emergency_liquidate(self) -> str:
        results = self.risk_engine.execute_emergency_liquidation()
        self.risk_engine.set_hard_lock("Emergency liquidation triggered by operator via Telegram")
        return f"🚨 *EMERGENCY LIQUIDATION ENGAGED*: Closed {len(results)} positions. Persistent HALTED.lock created."

    def handle_resume(self) -> str:
        if self.risk_engine.release_hard_lock():
            return "✅ *TRADING RESUMED*: HALTED.lock cleared by operator. Desk returned to active state."
        return "ℹ️ *TRADING RESUME*: No active HALTED.lock file was found."

    # -------------------------------------------------------------
    # 2. Interactive HITL Deadlock Escalation Card
    # -------------------------------------------------------------
    def format_hitl_escalation_card(
        self,
        candidate: ScreenedCandidate,
        verdict: CommitteeVerdict,
        deliberations: List[LLMDeliberation],
    ) -> Dict[str, Any]:
        """
        Formats an interactive escalation card when 51% vs 49% deadlock occurs.
        Returns text payload and inline button layout.
        """
        sent_delib = next((d for d in deliberations if "sentiment" in d.agent_role.value), None)
        tech_delib = next((d for d in deliberations if "technical" in d.agent_role.value), None)
        risk_delib = next((d for d in deliberations if "risk" in d.agent_role.value), None)

        shares = candidate.allocated_usd / candidate.entry_est if candidate.entry_est > 0 else 0.0
        sl_pct = (
            ((candidate.stop_loss - candidate.entry_est) / candidate.entry_est) * 100.0
            if candidate.entry_est > 0
            else 0.0
        )
        tp_pct = (
            ((candidate.take_profit - candidate.entry_est) / candidate.entry_est) * 100.0
            if candidate.entry_est > 0
            else 0.0
        )

        text = (
            f"⚖️ *[TRADE DELIBERATION SPLIT: {candidate.ticker}]*\n"
            f"Strategy: `{candidate.strategy.value}`\n"
            f"Target Sizing: `${candidate.allocated_usd:.2f}` (~{shares:.4f} shares @ `${candidate.entry_est:.2f}`)\n"
            f"Bracket: SL `${candidate.stop_loss:.2f}` ({sl_pct:+.1f}%) | TP `${candidate.take_profit:.2f}` ({tp_pct:+.1f}%)\n"
            f"Risk / Reward: `2.0R` (${candidate.risk_r:.2f} risk/share)\n\n"
            f"--- *ANALYST CASES* ---\n"
            f"🟢 Sentiment (25.5%): `{sent_delib.score_10 if sent_delib else 0:.1f}/10` - {sent_delib.rationale_summary if sent_delib else 'N/A'}\n"
            f"🟢 Technical (25.5%): `{tech_delib.score_10 if tech_delib else 0:.1f}/10` - {tech_delib.rationale_summary if tech_delib else 'N/A'}\n"
            f"🔴 Risk Officer (49.0%): `{risk_delib.score_10 if risk_delib else 0:.1f}/10` [{risk_delib.stance.value if risk_delib else 'VETO'}] - {risk_delib.rationale_summary if risk_delib else 'N/A'}\n\n"
            f"*Status*: HITL DEADLOCK (51% Bullish vs 49% Risk Dissent)\n"
            f"Awaiting Human Operator Decision (15-min fail-safe timeout):"
        )

        buttons = [
            [
                {
                    "text": f"Approve Trade (${candidate.allocated_usd:.2f})",
                    "callback_data": f"hitl_approve:{candidate.candidate_id}",
                },
                {
                    "text": "Reject Trade",
                    "callback_data": f"hitl_reject:{candidate.candidate_id}",
                },
            ]
        ]

        return {
            "text": text,
            "reply_markup": {"inline_keyboard": buttons},
            "candidate_id": candidate.candidate_id,
        }

    # -------------------------------------------------------------
    # 3. Callback Query Handler
    # -------------------------------------------------------------
    def handle_callback(self, callback_data: str) -> Tuple[bool, str]:
        """
        Executes action when human clicks inline button:
        'hitl_approve:<candidate_id>' or 'hitl_reject:<candidate_id>'
        """
        action, _, cand_id = callback_data.partition(":")
        candidate = self.db.get_candidate(cand_id)

        if not candidate:
            return False, f"Candidate {cand_id} not found."

        if action == "hitl_approve":
            self.db.update_hitl_verdict(cand_id, HITLStatus.HUMAN_APPROVED)
            self.db.update_candidate_status(cand_id, CandidateStatus.APPROVED)
            ok, order, res, msg = self.risk_engine.validate_and_route_order(candidate)
            if ok:
                return True, f"✅ Trade APPROVED by Operator for {candidate.ticker}. Routed to broker."
            else:
                return False, f"⚠️ Approved by Operator but Risk Engine blocked execution: {msg}"

        elif action == "hitl_reject":
            self.db.update_hitl_verdict(cand_id, HITLStatus.HUMAN_REJECTED)
            self.db.update_candidate_status(cand_id, CandidateStatus.VETOED)
            return True, f"❌ Trade REJECTED by Operator for {candidate.ticker}."

        return False, "Unknown callback action."
