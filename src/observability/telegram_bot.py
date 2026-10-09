"""
Telegram Observability Bot, Transport Adapter & HITL Escalation Daemon.
Implements:
- TelegramTransportAdapter: HTTP transport for Telegram Bot API with error auditing and retry/mocking support.
- Core commands: /status, /positions, /journal [ticker], /soft_freeze, /emergency_liquidate, /resume
- Interactive HITL Escalation Card formatting with inline action buttons
- Callback button handling for human trade approval/rejection with deduplication & replay protection
- Sender authorization (TELEGRAM_ALLOWED_USER_IDS)
- Webhook / polling update processor
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import requests

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


class TelegramTransportError(Exception):
    """Raised when Telegram HTTP transport fails."""
    pass


class TelegramTransportAdapter:
    """
    HTTP transport adapter for Telegram Bot API.
    Handles send_message, answer_callback_query, get_updates, and set_webhook.
    Audits transport errors into SQLite database.
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        session: Optional[requests.Session] = None,
        db: Optional[Database] = None,
        base_url: str = "https://api.telegram.org",
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.session = session or requests.Session()
        self.db = db
        self.base_url = base_url.rstrip("/")

    @property
    def api_url(self) -> str:
        return f"{self.base_url}/bot{self.bot_token}"

    def _request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        url = f"{self.api_url}/{endpoint.lstrip('/')}"
        try:
            resp = self.session.request(
                method=method,
                url=url,
                json=json_data,
                params=params,
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                desc = data.get("description", "Unknown Telegram API error")
                err_msg = f"Telegram API error {endpoint}: {desc}"
                self._record_error(err_msg, {"endpoint": endpoint, "response": data})
                raise TelegramTransportError(err_msg)
            return data
        except requests.RequestException as e:
            err_msg = f"Telegram HTTP transport failure {endpoint}: {e}"
            self._record_error(err_msg, {"endpoint": endpoint, "error": str(e)})
            raise TelegramTransportError(err_msg) from e

    def _record_error(self, message: str, metadata: Dict[str, Any]) -> None:
        if self.db is not None:
            try:
                self.db.save_audit_log(
                    severity=AuditSeverity.ERROR,
                    component="TelegramTransport",
                    event_name="TELEGRAM_TRANSPORT_ERROR",
                    message=message,
                    metadata=metadata,
                )
            except Exception:
                pass

    def send_message(
        self,
        chat_id: Union[str, int],
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: str = "Markdown",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._request("POST", "sendMessage", json_data=payload)

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text
        return self._request("POST", "answerCallbackQuery", json_data=payload)

    def get_updates(
        self,
        offset: Optional[int] = None,
        timeout: int = 30,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"timeout": timeout, "limit": limit}
        if offset is not None:
            params["offset"] = offset
        res = self._request("GET", "getUpdates", params=params, timeout=timeout + 5)
        return res.get("result", [])

    def set_webhook(
        self,
        url: str,
        secret_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"url": url}
        if secret_token:
            payload["secret_token"] = secret_token
        return self._request("POST", "setWebhook", json_data=payload)


class TelegramBotHandler:
    def __init__(
        self,
        db: Database,
        broker: AbstractBrokerAdapter,
        risk_engine: RiskEngine,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        allowed_user_ids: Optional[Union[List[str], Set[str], str]] = None,
        transport: Optional[TelegramTransportAdapter] = None,
    ):
        self.db = db
        self.broker = broker
        self.risk_engine = risk_engine
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")

        # Configure allowed user IDs for sender authorization
        if allowed_user_ids is not None:
            if isinstance(allowed_user_ids, str):
                self.allowed_user_ids: Optional[Set[str]] = {
                    uid.strip() for uid in allowed_user_ids.split(",") if uid.strip()
                }
            else:
                self.allowed_user_ids = {str(uid).strip() for uid in allowed_user_ids if str(uid).strip()}
        else:
            env_allowed = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
            if env_allowed:
                self.allowed_user_ids = {uid.strip() for uid in env_allowed.split(",") if uid.strip()}
            else:
                self.allowed_user_ids = None

        self.transport = transport
        self.processed_callback_ids: Set[str] = set()
        self.update_offset: int = 0

    # -------------------------------------------------------------
    # 0. Authorization & Security
    # -------------------------------------------------------------
    def is_user_authorized(self, user_id: Optional[Union[str, int]]) -> bool:
        """
        Validates if the user_id is authorized to execute commands/actions.
        If allowed_user_ids is configured, rejects unauthorized callers and logs audit event.
        """
        if self.allowed_user_ids is None:
            return True

        if user_id is None:
            self.db.save_audit_log(
                severity=AuditSeverity.WARNING,
                component="TelegramBot",
                event_name="UNAUTHORIZED_TELEGRAM_ACCESS",
                message="Unauthorized access attempt: missing user ID",
            )
            return False

        uid_str = str(user_id).strip()
        if uid_str not in self.allowed_user_ids:
            self.db.save_audit_log(
                severity=AuditSeverity.WARNING,
                component="TelegramBot",
                event_name="UNAUTHORIZED_TELEGRAM_ACCESS",
                message=f"Unauthorized access attempt from user_id: {uid_str}",
                metadata={"user_id": uid_str},
            )
            return False

        return True

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

    def handle_soft_freeze(self, actor: str = "telegram_operator") -> str:
        self.risk_engine.engage_soft_freeze(actor=actor, reason="Operator initiated soft freeze via Telegram command")
        return "⚠️ *SOFT FREEZE ENGAGED*: New buy orders halted. Active positions will run to bracket stops."

    def handle_emergency_liquidate(self) -> str:
        results = self.risk_engine.execute_emergency_liquidation()
        self.risk_engine.set_hard_lock("Emergency liquidation triggered by operator via Telegram")
        return f"🚨 *EMERGENCY LIQUIDATION ENGAGED*: Closed {len(results)} positions. Persistent HALTED.lock created."

    def handle_resume(self, actor: str = "telegram_operator") -> str:
        hard_cleared = self.risk_engine.release_hard_lock()
        soft_cleared = False
        if self.risk_engine.is_soft_freeze_active():
            self.risk_engine.release_soft_freeze(actor=actor, reason="Operator resumed trading via Telegram")
            soft_cleared = True

        if hard_cleared and soft_cleared:
            return "✅ *TRADING RESUMED*: HALTED.lock and soft freeze cleared by operator. Desk returned to active state."
        elif hard_cleared:
            return "✅ *TRADING RESUMED*: HALTED.lock cleared by operator. Desk returned to active state."
        elif soft_cleared:
            return "✅ *TRADING RESUMED*: Soft freeze cleared by operator. Desk returned to active state."
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
    # 3. Callback Query Handler (with Replay Protection)
    # -------------------------------------------------------------
    def handle_callback(self, callback_data: str) -> Tuple[bool, str]:
        """
        Executes action when human clicks inline button:
        'hitl_approve:<candidate_id>' or 'hitl_reject:<candidate_id>'
        Replay protection: harmless if already approved, rejected, or resolved.
        """
        action, _, cand_id = callback_data.partition(":")
        candidate = self.db.get_candidate(cand_id)

        if not candidate:
            return False, f"Candidate {cand_id} not found."

        # Check existing candidate status and verdict for replay protection
        verdict = self.db.get_committee_verdict(cand_id)
        if verdict and verdict.hitl_status in (
            HITLStatus.HUMAN_APPROVED,
            HITLStatus.HUMAN_REJECTED,
            HITLStatus.TIMED_OUT,
        ):
            return True, f"ℹ️ Callback replay harmless: candidate {cand_id} already resolved ({verdict.hitl_status.value})."

        if candidate.status in (
            CandidateStatus.APPROVED,
            CandidateStatus.VETOED,
            CandidateStatus.EXPIRED,
        ):
            return True, f"ℹ️ Callback replay harmless: candidate {cand_id} already in terminal state ({candidate.status.value})."

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

    # -------------------------------------------------------------
    # 4. Dispatchers & Update Processing
    # -------------------------------------------------------------
    def dispatch_command(
        self,
        command_text: str,
        user_id: Optional[Union[str, int]] = None,
    ) -> str:
        """Dispatches text commands with sender authorization."""
        if not self.is_user_authorized(user_id):
            return "⛔ Unauthorized: Access denied."

        parts = command_text.strip().split()
        if not parts:
            return "Invalid command."

        cmd = parts[0].lower()
        if cmd.startswith("/"):
            cmd = cmd[1:]
        # Remove bot suffix if any, e.g., /status@MyBot -> status
        cmd = cmd.split("@")[0]

        arg = parts[1] if len(parts) > 1 else None

        if cmd == "status":
            return self.handle_status()
        elif cmd == "positions":
            return self.handle_positions()
        elif cmd == "journal":
            return self.handle_journal(arg)
        elif cmd == "soft_freeze":
            return self.handle_soft_freeze(actor=f"telegram:{user_id or 'operator'}")
        elif cmd == "emergency_liquidate":
            return self.handle_emergency_liquidate()
        elif cmd == "resume":
            return self.handle_resume(actor=f"telegram:{user_id or 'operator'}")
        else:
            return f"Unknown command: /{cmd}. Available: /status, /positions, /journal, /soft_freeze, /emergency_liquidate, /resume"

    def dispatch_callback(
        self,
        callback_query_id: str,
        callback_data: str,
        user_id: Optional[Union[str, int]] = None,
    ) -> Tuple[bool, str]:
        """
        Dispatches callback query with authorization, deduplication, and acknowledgement.
        """
        if not self.is_user_authorized(user_id):
            if self.transport:
                try:
                    self.transport.answer_callback_query(
                        callback_query_id,
                        text="⛔ Unauthorized user.",
                        show_alert=True,
                    )
                except Exception:
                    pass
            return False, "⛔ Unauthorized user."

        # Replay protection by callback_query_id
        if callback_query_id in self.processed_callback_ids:
            if self.transport:
                try:
                    self.transport.answer_callback_query(
                        callback_query_id,
                        text="Action already processed.",
                    )
                except Exception:
                    pass
            return True, "Callback query already processed (replay harmless)."

        ok, msg = self.handle_callback(callback_data)
        self.processed_callback_ids.add(callback_query_id)

        if self.transport:
            try:
                self.transport.answer_callback_query(
                    callback_query_id,
                    text="Decision recorded." if ok else "Action rejected.",
                )
            except Exception:
                pass

        return ok, msg

    def process_update(self, update: Dict[str, Any]) -> Dict[str, Any]:
        """
        Processes a raw Telegram Update dict (webhook or polling).
        Returns a structured outcome dict.
        """
        if "message" in update:
            msg = update["message"]
            user_id = msg.get("from", {}).get("id")
            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text", "")
            response = self.dispatch_command(text, user_id=user_id)
            if self.transport and chat_id:
                try:
                    self.transport.send_message(chat_id=chat_id, text=response)
                except Exception:
                    pass
            return {"type": "message", "user_id": user_id, "response": response}

        elif "callback_query" in update:
            cq = update["callback_query"]
            cq_id = str(cq.get("id", ""))
            data = str(cq.get("data", ""))
            user_id = cq.get("from", {}).get("id")
            ok, msg = self.dispatch_callback(cq_id, data, user_id=user_id)
            return {"type": "callback_query", "user_id": user_id, "ok": ok, "message": msg}

        return {"type": "ignored", "reason": "unsupported_update_type"}

    def poll_once(self, timeout: int = 0) -> List[Dict[str, Any]]:
        """
        Polls updates once via transport, dispatches each, and updates offset.
        """
        if not self.transport:
            return []

        updates = self.transport.get_updates(offset=self.update_offset, timeout=timeout)
        results: List[Dict[str, Any]] = []
        for u in updates:
            up_id = u.get("update_id")
            if up_id is not None:
                self.update_offset = max(self.update_offset, up_id + 1)
            results.append(self.process_update(u))
        return results
