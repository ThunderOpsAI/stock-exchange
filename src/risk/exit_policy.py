"""
Exit Policy Engine (P3-05).
Implements time stops, trailing exits, pre-earnings liquidation, and gap protection.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

from src.broker.base import AbstractBrokerAdapter
from src.domain.models import AuditSeverity, ExitReason, Position, PositionStatus
from src.storage.db import Database

logger = logging.getLogger(__name__)


class ExitPolicyConfig(BaseModel):
    max_holding_days: int = 10  # Maximum days to hold a swing position
    trailing_stop_activation_pct: float = 0.05  # 5% gain activates trailing stop
    trailing_stop_distance_pct: float = 0.03  # Trails 3% behind high-water mark
    pre_earnings_exit_days: float = 1.0  # Liquidate 24h prior to known earnings


class ExitPolicyManager:
    def __init__(
        self,
        db: Database,
        broker: AbstractBrokerAdapter,
        config: Optional[ExitPolicyConfig] = None,
    ):
        self.db = db
        self.broker = broker
        self.config = config or ExitPolicyConfig()
        self.high_water_marks: Dict[str, float] = {}

    def update_high_water_mark(self, position_id: str, price: float) -> float:
        """Updates and returns the highest price observed for this position."""
        current_hwm = self.high_water_marks.get(position_id, 0.0)
        new_hwm = max(current_hwm, price)
        self.high_water_marks[position_id] = new_hwm
        return new_hwm

    def evaluate_position_exit(
        self,
        position: Position,
        current_price: Optional[float],
        as_of: Optional[datetime] = None,
    ) -> Tuple[bool, Optional[ExitReason], float, str]:
        """
        Evaluates deterministic exit conditions for an active position:
        1. Pre-earnings mandatory liquidation (within 24h of earnings)
        2. Time stop (holding period >= max_holding_days)
        3. Gap stop (open/current price < stop_loss level)
        4. Trailing stop (activated when peak gain >= 5%, trails 3% from peak)
        5. Bracket Stop-loss & Take-profit
        6. Missing quote handling (fails closed or maintains current state)
        """
        now = as_of or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        pos_id = position.position_id
        ticker = position.ticker.upper()

        # 1. Pre-earnings liquidation check
        meta = self.db.get_instrument_metadata(ticker)
        if meta and meta.get("next_earnings_date"):
            next_earn = meta["next_earnings_date"]
            earn_dt = datetime.fromisoformat(next_earn) if isinstance(next_earn, str) else next_earn
            if earn_dt.tzinfo is None:
                earn_dt = earn_dt.replace(tzinfo=timezone.utc)
            days_to_earnings = (earn_dt - now).total_seconds() / 86400.0
            if 0.0 <= days_to_earnings <= self.config.pre_earnings_exit_days:
                exec_price = current_price if (current_price and current_price > 0) else position.current_price
                return (
                    True,
                    ExitReason.EARNINGS_PRE_EXIT,
                    exec_price,
                    f"Mandatory pre-earnings liquidation: earnings in {days_to_earnings:.2f} days",
                )

        # 2. Time Stop check (holding duration)
        opened_at = position.opened_at
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        holding_days = (now - opened_at).total_seconds() / 86400.0
        if holding_days >= self.config.max_holding_days:
            exec_price = current_price if (current_price and current_price > 0) else position.current_price
            return (
                True,
                ExitReason.TIME_STOP,
                exec_price,
                f"Time stop triggered: holding duration {holding_days:.1f} days >= {self.config.max_holding_days} days",
            )

        # 3. Missing quote check
        if current_price is None or current_price <= 0.0:
            return False, None, position.current_price, "Missing or invalid price quote: cannot evaluate price-based exits"

        # Update high-water mark for trailing stop
        hwm = self.update_high_water_mark(pos_id, current_price)

        # 4. Gap stop & Standard stop-loss
        if position.stop_loss and current_price <= position.stop_loss:
            if current_price < position.stop_loss:
                return (
                    True,
                    ExitReason.GAP_STOP,
                    current_price,
                    f"Overnight gap stop triggered: price ${current_price:.2f} gapped below SL ${position.stop_loss:.2f}",
                )
            return (
                True,
                ExitReason.STOP_LOSS,
                current_price,
                f"Stop-loss target hit at ${current_price:.2f}",
            )

        # 5. Trailing stop check
        # Activates once position gains at least trailing_stop_activation_pct (5%)
        gain_from_entry = (hwm - position.entry_price) / position.entry_price
        if gain_from_entry >= self.config.trailing_stop_activation_pct:
            trail_level = round(hwm * (1.0 - self.config.trailing_stop_distance_pct), 2)
            if current_price <= trail_level and trail_level > position.entry_price:
                return (
                    True,
                    ExitReason.TRAILING_STOP,
                    current_price,
                    f"Trailing stop triggered: price ${current_price:.2f} crossed trail level ${trail_level:.2f} (peak: ${hwm:.2f})",
                )

        # 6. Take profit check
        if position.take_profit and current_price >= position.take_profit:
            return (
                True,
                ExitReason.TAKE_PROFIT,
                current_price,
                f"Take-profit target hit at ${current_price:.2f}",
            )

        return False, None, current_price, "Position within normal operating parameters"

    def run_exit_policy(
        self,
        current_prices: Dict[str, float],
        as_of: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Audits all open positions against deterministic exit policy rules and executes exits.
        Returns list of executed exit records.
        """
        now = as_of or datetime.now(timezone.utc)
        open_positions = self.broker.get_positions()
        executed_exits = []

        for pos in open_positions:
            price = current_prices.get(pos.ticker.upper())
            should_exit, reason, exit_price, explanation = self.evaluate_position_exit(
                position=pos, current_price=price, as_of=now
            )

            if should_exit and reason:
                res = self.broker.close_position(pos.position_id)
                realized = round((exit_price - pos.entry_price) * pos.qty, 2)

                # Update database position
                db_pos = self.db.get_position(pos.position_id)
                if db_pos:
                    db_pos.status = PositionStatus.CLOSED
                    db_pos.closed_at = now
                    db_pos.current_price = exit_price
                    db_pos.realized_pnl = realized
                    db_pos.exit_reason = reason
                    self.db.update_position(db_pos)

                self.high_water_marks.pop(pos.position_id, None)

                self.db.save_audit_log(
                    severity=AuditSeverity.INFO,
                    component="ExitPolicy",
                    event_name="EXIT_EXECUTED",
                    message=f"Exit executed for {pos.ticker} at ${exit_price:.2f} ({reason.value}): {explanation}",
                    metadata={
                        "position_id": pos.position_id,
                        "ticker": pos.ticker,
                        "exit_reason": reason.value,
                        "exit_price": exit_price,
                        "realized_pnl": realized,
                        "explanation": explanation,
                    },
                )
                executed_exits.append(
                    {
                        "position_id": pos.position_id,
                        "ticker": pos.ticker,
                        "exit_reason": reason.value,
                        "exit_price": exit_price,
                        "realized_pnl": realized,
                        "explanation": explanation,
                    }
                )

        return executed_exits
