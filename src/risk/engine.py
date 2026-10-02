"""
Deterministic Risk Engine & Hard Circuit Breaker.
Strictly enforces:
- Two-Tier Circuit Breaker: Soft buy halt (Equity <= $80.00); Hard emergency liquidation (Equity <= $70.00 + HALTED.lock).
- Position sizing: Max 3 slots, $10.00 permanent cash buffer, max $3.00 loss risk cap per trade.
- Order state machine transitions.
- Dual-layer bracket watchdog on bar close.
"""

from __future__ import annotations

import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.broker.base import AbstractBrokerAdapter
from src.domain.models import (
    AuditSeverity,
    CircuitBreakerTier,
    ExitReason,
    Order,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
    PortfolioSnapshot,
    Position,
    PositionStatus,
    ScreenedCandidate,
)
from src.storage.db import Database

LOCK_FILE_PATH = Path("HALTED.lock")


class RiskEngine:
    def __init__(
        self,
        db: Database,
        broker: AbstractBrokerAdapter,
        max_slots: int = 3,
        slot_target_usd: float = 30.0,
        cash_buffer_usd: float = 10.0,
        max_risk_cap_usd: float = 3.0,
        soft_halt_equity: float = 80.0,
        hard_liquidation_equity: float = 70.0,
        lock_file: Path = LOCK_FILE_PATH,
    ):
        self.db = db
        self.broker = broker
        self.max_slots = max_slots
        self.slot_target_usd = slot_target_usd
        self.cash_buffer_usd = cash_buffer_usd
        self.max_risk_cap_usd = max_risk_cap_usd
        self.soft_halt_equity = soft_halt_equity
        self.hard_liquidation_equity = hard_liquidation_equity
        self.lock_file = lock_file

    def is_hard_locked(self) -> bool:
        """Returns True if the persistent emergency HALTED.lock file exists."""
        return self.lock_file.exists()

    def set_hard_lock(self, reason: str) -> None:
        """Creates the persistent HALTED.lock file."""
        with open(self.lock_file, "w") as f:
            f.write(f"TIMESTAMP: {datetime.now(timezone.utc).isoformat()}\nREASON: {reason}\n")
        self.db.save_audit_log(
            severity=AuditSeverity.CRITICAL,
            component="RiskEngine",
            event_name="EMERGENCY_LOCK_ENGAGED",
            message=f"Hard emergency lock engaged: {reason}",
        )

    def release_hard_lock(self) -> bool:
        """Removes the persistent HALTED.lock file (operator reset)."""
        if self.lock_file.exists():
            self.lock_file.unlink()
            self.db.save_audit_log(
                severity=AuditSeverity.INFO,
                component="RiskEngine",
                event_name="EMERGENCY_LOCK_RELEASED",
                message="Hard emergency lock released by operator.",
            )
            return True
        return False

    def evaluate_circuit_breaker(self, equity: float) -> CircuitBreakerTier:
        """
        Evaluates equity against the two-tier circuit breaker:
        Tier 0: Normal (> $80.00)
        Tier 1: Soft Buy Halt (<= $80.00 and > $70.00)
        Tier 2: Hard Liquidation Floor (<= $70.00 or HALTED.lock present)
        """
        if self.is_hard_locked():
            return CircuitBreakerTier.HARD_LIQUIDATION

        if equity <= self.hard_liquidation_equity:
            self.set_hard_lock(f"Equity (${equity:.2f}) breached hard floor (${self.hard_liquidation_equity:.2f})")
            return CircuitBreakerTier.HARD_LIQUIDATION
        elif equity <= self.soft_halt_equity:
            return CircuitBreakerTier.SOFT_HALT

        return CircuitBreakerTier.NORMAL

    def calculate_position_size(
        self, candidate: ScreenedCandidate, current_cash: float
    ) -> Tuple[bool, float, float, str]:
        """
        Calculates position sizing adhering to:
        1. Max 3 slots.
        2. Permanent $10.00 cash buffer.
        3. Max $3.00 risk cap: Capital = min($30.00, AvailableCash, ($3.00 * Entry) / (Entry - SL))
        4. Fractional shares floored to 4 decimals.
        Returns: (approved, capital_usd, qty, reason)
        """
        available_cash = max(0.0, current_cash - self.cash_buffer_usd)
        if available_cash < 10.0:  # Minimum viable entry
            return False, 0.0, 0.0, f"Insufficient cash above $10 buffer (Available: ${available_cash:.2f})"

        entry = candidate.entry_est
        sl = candidate.stop_loss
        risk_per_share = entry - sl

        if risk_per_share <= 0:
            return False, 0.0, 0.0, "Invalid stop loss level: SL >= Entry"

        # Risk-capped capital formula
        capital_by_risk = (self.max_risk_cap_usd * entry) / risk_per_share
        allocated_usd = min(self.slot_target_usd, available_cash, capital_by_risk)

        if allocated_usd < 10.0:
            return False, 0.0, 0.0, f"Sized allocation ${allocated_usd:.2f} is below $10.00 broker minimum"

        # Calculate fractional qty floored to 4 decimals
        raw_qty = allocated_usd / entry
        qty = math.floor(raw_qty * 10000) / 10000.0

        if qty <= 0:
            return False, 0.0, 0.0, "Calculated quantity is 0"

        final_allocated = round(qty * entry, 2)
        return True, final_allocated, qty, "Sizing approved"

    def execute_emergency_liquidation(self) -> List[OrderResult]:
        """Liquidates all open positions immediately upon Tier 2 breach or manual command."""
        open_positions = self.broker.get_positions()
        results = []
        for pos in open_positions:
            res = self.broker.close_position(pos.position_id)
            results.append(res)
            # Update position in db
            db_pos = self.db.get_position(pos.position_id)
            if db_pos:
                db_pos.status = PositionStatus.CLOSED
                db_pos.closed_at = datetime.now(timezone.utc)
                db_pos.exit_reason = ExitReason.CIRCUIT_BREAKER_HALT
                self.db.update_position(db_pos)

        self.db.save_audit_log(
            severity=AuditSeverity.CRITICAL,
            component="RiskEngine",
            event_name="CIRCUIT_BREAKER_TIER2_LIQUIDATION",
            message=f"Emergency liquidation executed across {len(open_positions)} positions.",
            metadata={"liquidated_count": len(open_positions)},
        )
        return results

    def validate_and_route_order(
        self, candidate: ScreenedCandidate
    ) -> Tuple[bool, Optional[Order], Optional[OrderResult], str]:
        """
        End-to-end validation of candidate against risk rules:
        - Check hard lock
        - Check circuit breaker tier
        - Check available slots
        - Check cash buffer & risk cap
        - Create Order in PENDING_RISK_CHECK state
        - Transition to RISK_APPROVED -> ROUTED_TO_BROKER -> FILLED
        """
        # 1. Hard lock check
        if self.is_hard_locked():
            return False, None, None, "Execution blocked: System is in HALTED.lock state."

        # 2. Balance & Circuit Breaker Tier
        balance = self.broker.get_account_balance()
        tier = self.evaluate_circuit_breaker(balance.equity)

        if tier == CircuitBreakerTier.HARD_LIQUIDATION:
            self.execute_emergency_liquidation()
            return False, None, None, "Execution blocked: Hard liquidation triggered."

        if tier == CircuitBreakerTier.SOFT_HALT:
            return False, None, None, f"Execution blocked: Soft Freeze active (Equity ${balance.equity:.2f} <= $80.00)."

        # 3. Check Slot Contention
        open_positions = self.broker.get_positions()
        if len(open_positions) >= self.max_slots:
            return False, None, None, f"Execution blocked: All {self.max_slots} slots currently occupied."

        # Check ticker duplicate
        for p in open_positions:
            if p.ticker == candidate.ticker.upper():
                return False, None, None, f"Execution blocked: Position for {candidate.ticker} already exists."

        # 4. Sizing calculation
        ok, allocated_usd, target_qty, size_reason = self.calculate_position_size(
            candidate, balance.cash
        )
        if not ok:
            return False, None, None, f"Risk sizing rejected: {size_reason}"

        # 5. Order state machine creation
        order_id = f"ord_{uuid.uuid4().hex[:8]}"
        client_order_id = f"client_{order_id}"
        order = Order(
            order_id=order_id,
            client_order_id=client_order_id,
            candidate_id=candidate.candidate_id,
            ticker=candidate.ticker.upper(),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            allocated_usd=allocated_usd,
            target_qty=target_qty,
            stop_loss=candidate.stop_loss,
            take_profit=candidate.take_profit,
            state=OrderState.PENDING_RISK_CHECK,
        )
        self.db.save_order(order)

        # Transition to RISK_APPROVED
        self.db.update_order_state(order_id, OrderState.RISK_APPROVED)
        order.state = OrderState.RISK_APPROVED

        # 6. Route to Broker
        req = OrderRequest(
            ticker=candidate.ticker.upper(),
            side=OrderSide.BUY,
            dollar_amount=allocated_usd,
            target_qty=target_qty,
            stop_loss=candidate.stop_loss,
            take_profit=candidate.take_profit,
            client_order_id=client_order_id,
            candidate_id=candidate.candidate_id,
        )
        order_res = self.broker.submit_order(req)

        if order_res.status in (OrderState.FILLED, OrderState.ROUTED_TO_BROKER):
            self.db.update_order_state(order_id, order_res.status)
            order.state = order_res.status

            # If broker position created, record position in DB
            broker_pos = self.broker.get_position(candidate.ticker.upper())
            if broker_pos:
                self.db.save_position(broker_pos)

            self.db.save_audit_log(
                severity=AuditSeverity.INFO,
                component="RiskEngine",
                event_name="ORDER_ROUTED_SUCCESS",
                message=f"Order {order_id} routed for {candidate.ticker} (${allocated_usd:.2f})",
            )
            return True, order, order_res, "Order successfully routed to broker"
        else:
            self.db.update_order_state(
                order_id, OrderState.REJECTED_BY_BROKER, rejection_reason=order_res.error_message
            )
            order.state = OrderState.REJECTED_BY_BROKER
            order.rejection_reason = order_res.error_message
            return False, order, order_res, f"Broker rejected order: {order_res.error_message}"

    def run_bracket_watchdog(
        self, current_prices: Dict[str, float]
    ) -> List[Tuple[str, str, float]]:
        """
        Bar close bracket watchdog:
        Audits all open positions against stop-loss and take-profit targets.
        Detects overnight gap-throughs and executes exits.
        Returns list of (ticker, exit_reason, exit_price).
        """
        open_positions = self.broker.get_positions()
        exits = []

        for pos in open_positions:
            price = current_prices.get(pos.ticker.upper(), pos.current_price)

            exit_reason = None
            if pos.stop_loss and price <= pos.stop_loss:
                # Check for gap-through stop
                exit_reason = ExitReason.STOP_LOSS
            elif pos.take_profit and price >= pos.take_profit:
                exit_reason = ExitReason.TAKE_PROFIT

            if exit_reason:
                res = self.broker.close_position(pos.position_id)
                exits.append((pos.ticker, exit_reason.value, price))

                # Update database
                db_pos = self.db.get_position(pos.position_id)
                if db_pos:
                    db_pos.status = PositionStatus.CLOSED
                    db_pos.closed_at = datetime.now(timezone.utc)
                    db_pos.current_price = price
                    db_pos.realized_pnl = round((price - db_pos.entry_price) * db_pos.qty, 2)
                    db_pos.exit_reason = exit_reason
                    self.db.update_position(db_pos)

                self.db.save_audit_log(
                    severity=AuditSeverity.INFO,
                    component="RiskEngine",
                    event_name="WATCHDOG_BRACKET_EXIT",
                    message=f"Watchdog closed position on {pos.ticker} at ${price:.2f} ({exit_reason.value})",
                )

        return exits

    def record_portfolio_snapshot(self) -> PortfolioSnapshot:
        """Captures and persists full portfolio snapshot."""
        bal = self.broker.get_account_balance()
        positions = self.broker.get_positions()
        tier = self.evaluate_circuit_breaker(bal.equity)

        invested = sum(p.market_value for p in positions)
        unrealized = sum(p.unrealized_pnl for p in positions)

        snap = PortfolioSnapshot(
            timestamp=datetime.now(timezone.utc),
            total_equity=bal.equity,
            cash_balance=bal.cash,
            invested_capital=round(invested, 2),
            unrealized_pnl=round(unrealized, 2),
            active_slots_used=len(positions),
            circuit_breaker_tier=tier,
        )
        self.db.save_portfolio_snapshot(snap)
        return snap
