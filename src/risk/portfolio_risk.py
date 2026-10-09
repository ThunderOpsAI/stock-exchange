"""
Portfolio Risk Limits & Exposure Engine (P3-03).
Enforces daily/weekly loss limits, consecutive-loss pause, and portfolio stop-risk caps.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from src.broker.base import AbstractBrokerAdapter
from src.domain.models import AuditSeverity
from src.storage.db import Database

logger = logging.getLogger(__name__)


class PortfolioRiskLimits(BaseModel):
    """Configurable portfolio risk thresholds calibrated for $100 capital."""
    daily_loss_limit_usd: float = 3.0  # 3% of capital
    weekly_loss_limit_usd: float = 6.0  # 6% of capital
    consecutive_loss_limit: int = 3    # Pause after 3 consecutive losses
    portfolio_stop_risk_cap_usd: float = 10.0  # Max total aggregate open risk ($10.00: 3 slots x $3 risk + slippage allowance)


class PortfolioRiskEvaluator:
    def __init__(
        self,
        db: Database,
        broker: AbstractBrokerAdapter,
        limits: Optional[PortfolioRiskLimits] = None,
    ):
        self.db = db
        self.broker = broker
        self.limits = limits or PortfolioRiskLimits()

    def get_daily_realized_losses(self, as_of: datetime) -> float:
        """Returns aggregate realized losses incurred since 00:00 UTC of as_of date."""
        start_of_day = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
        query = """
        SELECT realized_pnl FROM positions
        WHERE status = 'CLOSED' AND closed_at >= ? AND realized_pnl < 0
        """
        total_loss = 0.0
        with self.db.session() as conn:
            rows = conn.execute(query, (start_of_day.isoformat(),)).fetchall()
            for r in rows:
                total_loss += abs(float(r["realized_pnl"]))
        return round(total_loss, 2)

    def get_weekly_realized_losses(self, as_of: datetime) -> float:
        """Returns aggregate realized losses incurred over the past 7 days."""
        start_of_week = as_of - timedelta(days=7)
        query = """
        SELECT realized_pnl FROM positions
        WHERE status = 'CLOSED' AND closed_at >= ? AND realized_pnl < 0
        """
        total_loss = 0.0
        with self.db.session() as conn:
            rows = conn.execute(query, (start_of_week.isoformat(),)).fetchall()
            for r in rows:
                total_loss += abs(float(r["realized_pnl"]))
        return round(total_loss, 2)

    def get_consecutive_losses(self) -> int:
        """Returns the current streak of consecutive closed trades with realized_pnl < 0."""
        query = """
        SELECT realized_pnl FROM positions
        WHERE status = 'CLOSED'
        ORDER BY closed_at DESC, rowid DESC
        """
        streak = 0
        with self.db.session() as conn:
            rows = conn.execute(query).fetchall()
            for r in rows:
                pnl = float(r["realized_pnl"])
                if pnl < 0:
                    streak += 1
                else:
                    break
        return streak

    def get_open_stop_risk(self) -> float:
        """Calculates sum of dollar stop-loss risk across currently open positions."""
        open_positions = self.broker.get_positions()
        total_risk = 0.0
        for pos in open_positions:
            if pos.stop_loss and pos.stop_loss < pos.entry_price:
                per_share_risk = pos.entry_price - pos.stop_loss
                total_risk += per_share_risk * pos.qty
        return round(total_risk, 2)

    def evaluate_entry(
        self, candidate_risk_usd: float = 0.0, as_of: Optional[datetime] = None
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Evaluates an entry against all portfolio limits.
        Strictest breached limit wins and is recorded in the audit log.
        Returns: (allowed: bool, strictest_reason: Optional[str], metadata: Dict[str, Any])
        """
        now = as_of or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        daily_losses = self.get_daily_realized_losses(now)
        weekly_losses = self.get_weekly_realized_losses(now)
        consecutive_losses = self.get_consecutive_losses()
        open_stop_risk = self.get_open_stop_risk()
        projected_stop_risk = round(open_stop_risk + candidate_risk_usd, 2)

        violations: List[Tuple[int, str]] = []

        # 1. Daily Loss Limit (Priority 1)
        if daily_losses >= self.limits.daily_loss_limit_usd:
            violations.append(
                (
                    1,
                    f"DAILY_LOSS_LIMIT_EXCEEDED: Realized daily loss ${daily_losses:.2f} >= limit ${self.limits.daily_loss_limit_usd:.2f}",
                )
            )

        # 2. Weekly Loss Limit (Priority 2)
        if weekly_losses >= self.limits.weekly_loss_limit_usd:
            violations.append(
                (
                    2,
                    f"WEEKLY_LOSS_LIMIT_EXCEEDED: Realized weekly loss ${weekly_losses:.2f} >= limit ${self.limits.weekly_loss_limit_usd:.2f}",
                )
            )

        # 3. Consecutive Loss Pause (Priority 3)
        if consecutive_losses >= self.limits.consecutive_loss_limit:
            violations.append(
                (
                    3,
                    f"CONSECUTIVE_LOSS_PAUSE_ACTIVE: {consecutive_losses} consecutive losses >= limit {self.limits.consecutive_loss_limit}",
                )
            )

        # 4. Portfolio Stop-Risk Cap (Priority 4)
        if projected_stop_risk > self.limits.portfolio_stop_risk_cap_usd:
            violations.append(
                (
                    4,
                    f"PORTFOLIO_STOP_RISK_CAP_EXCEEDED: Projected stop risk ${projected_stop_risk:.2f} > cap ${self.limits.portfolio_stop_risk_cap_usd:.2f}",
                )
            )

        meta = {
            "daily_losses": daily_losses,
            "weekly_losses": weekly_losses,
            "consecutive_losses": consecutive_losses,
            "open_stop_risk": open_stop_risk,
            "candidate_risk_usd": candidate_risk_usd,
            "projected_stop_risk": projected_stop_risk,
            "limits": self.limits.model_dump(),
        }

        if violations:
            violations.sort(key=lambda x: x[0])
            strictest_reason = violations[0][1]
            self.db.save_audit_log(
                severity=AuditSeverity.WARNING,
                component="PortfolioRisk",
                event_name="PORTFOLIO_RISK_LIMIT_BREACHED",
                message=strictest_reason,
                metadata=meta,
            )
            return False, strictest_reason, meta

        self.db.save_audit_log(
            severity=AuditSeverity.INFO,
            component="PortfolioRisk",
            event_name="PORTFOLIO_RISK_PASSED",
            message=f"Portfolio risk checks passed (projected stop risk: ${projected_stop_risk:.2f})",
            metadata=meta,
        )
        return True, None, meta
