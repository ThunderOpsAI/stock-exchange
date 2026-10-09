"""
Human-In-The-Loop (HITL) Decision & Expiry Manager.
Enforces:
- Server-side timestamp verification against expiry timeout (default 15 minutes / 900s).
- Strict single terminal outcome (HUMAN_APPROVED, HUMAN_REJECTED, or TIMED_OUT).
- Deterministic risk engine validation still runs on human approval.
- Expiry sweeps and audit logging.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.domain.models import (
    AuditSeverity,
    CandidateStatus,
    CommitteeVerdict,
    HITLStatus,
    Order,
    ScreenedCandidate,
)
from src.risk.engine import RiskEngine
from src.storage.db import Database


class HITLDecisionManager:
    """
    Manages HITL escalation lifecycle, enforces timeout expiry with server-side
    timestamps, guarantees single terminal state transitions, and invokes
    the deterministic risk engine upon approval.
    """

    def __init__(
        self,
        db: Database,
        risk_engine: Optional[RiskEngine] = None,
        timeout_seconds: float = 900.0,  # 15 minutes default
    ):
        self.db = db
        self.risk_engine = risk_engine
        self.timeout_seconds = float(timeout_seconds)

    def _normalize_dt(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def check_expiry(
        self,
        candidate_id: str,
        as_of: Optional[datetime] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Checks if candidate's HITL escalation window has expired.
        If expired, transitions verdict to TIMED_OUT and candidate to EXPIRED.
        Returns (is_expired, explanation).
        """
        verdict = self.db.get_committee_verdict(candidate_id)
        if not verdict:
            return False, f"Candidate verdict not found for {candidate_id}."

        if verdict.hitl_status == HITLStatus.TIMED_OUT:
            return True, "HITL escalation window has already timed out."

        if verdict.hitl_status != HITLStatus.PENDING_TELEGRAM_RESPONSE:
            return False, f"Candidate already finalized with status: {verdict.hitl_status.value if verdict.hitl_status else 'None'}."

        now = self._normalize_dt(as_of or datetime.now(timezone.utc))
        verdict_time = self._normalize_dt(verdict.timestamp)
        elapsed = (now - verdict_time).total_seconds()

        if elapsed > self.timeout_seconds:
            # Transition to TIMED_OUT
            transitioned = self.db.transition_hitl_verdict(
                candidate_id=candidate_id,
                from_status=HITLStatus.PENDING_TELEGRAM_RESPONSE,
                to_status=HITLStatus.TIMED_OUT,
                responded_at=now,
            )
            if transitioned:
                self.db.update_candidate_status(candidate_id, CandidateStatus.EXPIRED)
                self.db.save_audit_log(
                    severity=AuditSeverity.WARNING,
                    component="HITLDecisionManager",
                    event_name="HITL_WINDOW_EXPIRED",
                    message=(
                        f"HITL escalation expired for candidate {candidate_id} ({verdict.ticker}) "
                        f"after {elapsed:.1f}s (timeout: {self.timeout_seconds:.1f}s)."
                    ),
                    metadata={
                        "candidate_id": candidate_id,
                        "ticker": verdict.ticker,
                        "elapsed_seconds": elapsed,
                        "timeout_seconds": self.timeout_seconds,
                    },
                )
            return True, f"HITL window expired ({elapsed:.1f}s > {self.timeout_seconds:.1f}s limit)."

        return False, None

    def sweep_expired(self, as_of: Optional[datetime] = None) -> List[str]:
        """
        Scans all pending HITL verdicts and transitions any that have exceeded the timeout window.
        Returns list of candidate IDs expired.
        """
        pending = self.db.get_pending_hitl_verdicts()
        expired_ids: List[str] = []
        for v in pending:
            is_exp, _ = self.check_expiry(v.candidate_id, as_of=as_of)
            if is_exp:
                expired_ids.append(v.candidate_id)
        return expired_ids

    def approve(
        self,
        candidate_id: str,
        actor: str = "operator",
        as_of: Optional[datetime] = None,
    ) -> Tuple[bool, str, Optional[Order]]:
        """
        Processes human approval for an escalated candidate:
        1. Verifies server-side timestamp against expiry. If expired, approval fails.
        2. Performs atomic state transition to HUMAN_APPROVED (fails if already transitioned).
        3. Executes deterministic Risk Engine validation and routing.
        """
        now = self._normalize_dt(as_of or datetime.now(timezone.utc))

        # Check expiry first
        is_expired, exp_reason = self.check_expiry(candidate_id, as_of=now)
        if is_expired:
            return False, f"Approve failed: {exp_reason}", None

        # Atomic transition to HUMAN_APPROVED
        transitioned = self.db.transition_hitl_verdict(
            candidate_id=candidate_id,
            from_status=HITLStatus.PENDING_TELEGRAM_RESPONSE,
            to_status=HITLStatus.HUMAN_APPROVED,
            responded_at=now,
        )
        if not transitioned:
            verdict = self.db.get_committee_verdict(candidate_id)
            current_status = verdict.hitl_status.value if verdict and verdict.hitl_status else "UNKNOWN"
            return (
                False,
                f"Approve failed: Candidate {candidate_id} is in non-pending terminal state ({current_status}).",
                None,
            )

        self.db.update_candidate_status(candidate_id, CandidateStatus.APPROVED)
        self.db.save_audit_log(
            severity=AuditSeverity.INFO,
            component="HITLDecisionManager",
            event_name="HITL_HUMAN_APPROVED",
            message=f"Operator '{actor}' approved trade candidate {candidate_id}.",
            metadata={"candidate_id": candidate_id, "actor": actor},
        )

        candidate = self.db.get_candidate(candidate_id)
        if not candidate:
            return False, f"Candidate data missing for {candidate_id}.", None

        # Deterministic Risk Engine evaluation
        if self.risk_engine is not None:
            ok, order, result, reason = self.risk_engine.validate_and_route_order(candidate)
            if not ok:
                self.db.save_audit_log(
                    severity=AuditSeverity.WARNING,
                    component="HITLDecisionManager",
                    event_name="HITL_APPROVAL_RISK_BLOCKED",
                    message=f"Human approved {candidate.ticker}, but Risk Engine blocked execution: {reason}",
                    metadata={"candidate_id": candidate_id, "reason": reason},
                )
                return False, f"Approved by operator, but Risk Engine blocked execution: {reason}", None

            return True, f"Trade approved and routed to broker (Order ID: {order.order_id if order else 'N/A'}).", order

        return True, "Trade approved by operator (no risk engine attached).", None

    def reject(
        self,
        candidate_id: str,
        actor: str = "operator",
        reason: str = "Operator vetoed trade",
        as_of: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """
        Processes human rejection for an escalated candidate:
        1. Verifies server-side timestamp against expiry. If expired, rejection fails.
        2. Performs atomic state transition to HUMAN_REJECTED (fails if already transitioned).
        """
        now = self._normalize_dt(as_of or datetime.now(timezone.utc))

        # Check expiry first
        is_expired, exp_reason = self.check_expiry(candidate_id, as_of=now)
        if is_expired:
            return False, f"Reject failed: {exp_reason}"

        # Atomic transition to HUMAN_REJECTED
        transitioned = self.db.transition_hitl_verdict(
            candidate_id=candidate_id,
            from_status=HITLStatus.PENDING_TELEGRAM_RESPONSE,
            to_status=HITLStatus.HUMAN_REJECTED,
            responded_at=now,
        )
        if not transitioned:
            verdict = self.db.get_committee_verdict(candidate_id)
            current_status = verdict.hitl_status.value if verdict and verdict.hitl_status else "UNKNOWN"
            return (
                False,
                f"Reject failed: Candidate {candidate_id} is in non-pending terminal state ({current_status}).",
            )

        self.db.update_candidate_status(candidate_id, CandidateStatus.VETOED)
        self.db.save_audit_log(
            severity=AuditSeverity.INFO,
            component="HITLDecisionManager",
            event_name="HITL_HUMAN_REJECTED",
            message=f"Operator '{actor}' rejected candidate {candidate_id}: {reason}",
            metadata={"candidate_id": candidate_id, "actor": actor, "reason": reason},
        )
        return True, f"Trade rejected by operator: {reason}"
