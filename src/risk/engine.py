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
        require_reconciled: bool = True,
        watchdog_max_cadence_seconds: float = 300.0,
        portfolio_risk_limits: Optional[PortfolioRiskLimits] = None,
        concentration_limits: Optional[ConcentrationRiskLimits] = None,
        exit_policy_config: Optional[ExitPolicyConfig] = None,
    ):
        self.require_reconciled = require_reconciled
        self.watchdog_max_cadence_seconds = watchdog_max_cadence_seconds
        self.last_watchdog_run_at: Optional[datetime] = None
        self.db = db
        self.broker = broker
        from src.risk.portfolio_risk import PortfolioRiskEvaluator
        from src.risk.concentration import ConcentrationRiskManager
        from src.risk.exit_policy import ExitPolicyManager
        self.portfolio_risk = PortfolioRiskEvaluator(db=self.db, broker=self.broker, limits=portfolio_risk_limits)
        self.concentration_manager = ConcentrationRiskManager(db=self.db, broker=self.broker, limits=concentration_limits)
        self.exit_policy = ExitPolicyManager(db=self.db, broker=self.broker, config=exit_policy_config)
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
        Tier 1: Soft Buy Halt (<= $80.00 and > $70.00, or persistent soft freeze active)
        Tier 2: Hard Liquidation Floor (<= $70.00 or HALTED.lock present)
        """
        if self.is_hard_locked():
            return CircuitBreakerTier.HARD_LIQUIDATION

        if equity <= self.hard_liquidation_equity:
            self.set_hard_lock(f"Equity (${equity:.2f}) breached hard floor (${self.hard_liquidation_equity:.2f})")
            return CircuitBreakerTier.HARD_LIQUIDATION
        elif self.is_soft_freeze_active() or equity <= self.soft_halt_equity:
            return CircuitBreakerTier.SOFT_HALT

        return CircuitBreakerTier.NORMAL

    def is_soft_freeze_active(self) -> bool:
        """Returns True if persistent soft freeze is active."""
        return self.db.is_soft_freeze_active()

    def get_soft_freeze_reason(self) -> str:
        """Returns the recorded reason for persistent soft freeze."""
        ctrl = self.db.get_soft_freeze_details()
        if ctrl and ctrl.get("reason"):
            return str(ctrl["reason"])
        return "Soft freeze active"

    def engage_soft_freeze(self, actor: str = "operator", reason: str = "Operator engaged soft freeze") -> None:
        """Engages persistent soft freeze, halting new buy orders while allowing exits to run."""
        self.db.set_soft_freeze(enabled=True, actor=actor, reason=reason)
        self.db.save_audit_log(
            severity=AuditSeverity.WARNING,
            component="RiskEngine",
            event_name="OPERATOR_SOFT_FREEZE_ENGAGED",
            message=f"Soft freeze engaged by {actor}: {reason}",
            metadata={"actor": actor, "reason": reason},
        )

    def release_soft_freeze(self, actor: str = "operator", reason: str = "Operator released soft freeze") -> None:
        """Releases persistent soft freeze, resuming normal entry evaluation."""
        self.db.set_soft_freeze(enabled=False, actor=actor, reason=reason)
        self.db.save_audit_log(
            severity=AuditSeverity.INFO,
            component="RiskEngine",
            event_name="OPERATOR_SOFT_FREEZE_RELEASED",
            message=f"Soft freeze released by {actor}: {reason}",
            metadata={"actor": actor, "reason": reason},
        )

    def check_reconciliation_and_pending(self) -> Tuple[bool, str]:
        """Fail-closed gate (ADR 0002): account must be reconciled and no unknown intents outstanding."""
        if self.require_reconciled:
            event = self.db.get_latest_reconciliation_event()
            if event is None or event.get("resolution_status") not in (
                "HEALTHY_MATCH",
                "RESOLVED_RECONCILED",
            ):
                return False, (
                    "Execution blocked: Account state is unreconciled or has unresolved "
                    "discrepancies (ADR 0002)"
                )
        for intent in self.db.get_pending_order_intents():
            if intent.get("status") == "UNKNOWN_PENDING_RECONCILIATION":
                return False, (
                    "Execution blocked: Order intent in UNKNOWN_PENDING_RECONCILIATION "
                    "state requires reconciliation"
                )
        return True, "OK"

    def reserved_cash(self) -> float:
        """Cash reserved by pending (CREATED/SUBMITTED/UNKNOWN) order intents."""
        return sum(float(i.get("allocated_usd") or 0.0) for i in self.db.get_pending_order_intents())

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
        if self.is_soft_freeze_active():
            reason = self.get_soft_freeze_reason()
            return False, 0.0, 0.0, f"SOFT_FREEZE_ACTIVE: {reason}"

        available_cash = max(0.0, current_cash - self.cash_buffer_usd - self.reserved_cash())
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

        if self.is_soft_freeze_active():
            reason = self.get_soft_freeze_reason()
            return False, None, None, f"SOFT_FREEZE_ACTIVE: {reason}"

        if tier == CircuitBreakerTier.SOFT_HALT:
            return False, None, None, f"Execution blocked: Soft Freeze active (Equity ${balance.equity:.2f} <= $80.00)."

        # 2b. Reconciliation / unknown-order gate
        gate_ok, gate_reason = self.check_reconciliation_and_pending()
        if not gate_ok:
            return False, None, None, gate_reason

        # 2c. Degraded-protection policy (ADR 0002 / P3-02)
        has_native = getattr(self.broker, "supports_native_bracket", False)
        watchdog_ok = self.is_watchdog_healthy()
        if not has_native and not watchdog_ok:
            return False, None, None, (
                "Execution blocked: Broker lacks native bracket protection and "
                "software watchdog is unhealthy or exceeds cadence threshold (ADR 0002)"
            )

        # Check if any active open position is in DEGRADED_UNPROTECTED state
        open_positions = self.broker.get_positions()
        for p in open_positions:
            prot = self.db.get_protection_status_for_position(p.position_id)
            if prot and (
                prot.get("protection_mode") == "DEGRADED_UNPROTECTED"
                or prot.get("watchdog_healthy") == 0
            ):
                return False, None, None, (
                    f"Execution blocked: Position for {p.ticker} is in DEGRADED_UNPROTECTED state (ADR 0002)"
                )

        # 3. Check Slot Contention (pending intents reserve slots)
        pending_intents = self.db.get_pending_order_intents()
        if len(open_positions) + len(pending_intents) >= self.max_slots:
            return False, None, None, (
                f"Execution blocked: All {self.max_slots} slots currently occupied "
                "or reserved by pending orders."
            )
        for intent in pending_intents:
            if str(intent.get("ticker", "")).upper() == candidate.ticker.upper():
                return False, None, None, (
                    f"Execution blocked: Pending order intent for {candidate.ticker} already exists."
                )

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

        # 4a. Concentration & Event-Risk checks (P3-04)
        conc_ok, conc_reason, _ = self.concentration_manager.evaluate_concentration(
            candidate.ticker, allocated_usd=allocated_usd
        )
        if not conc_ok:
            return False, None, None, f"Execution blocked: {conc_reason}"

        # 4b. Portfolio risk limits (daily/weekly losses, consecutive loss pause, stop risk cap - P3-03)
        risk_per_share = max(0.0, candidate.entry_est - candidate.stop_loss)
        cand_risk_usd = round(risk_per_share * target_qty, 2)
        pr_ok, pr_reason, _ = self.portfolio_risk.evaluate_entry(cand_risk_usd)
        if not pr_ok:
            return False, None, None, f"Execution blocked: {pr_reason}"

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
                from src.broker.protection import BrokerProtectionService
                prot_svc = BrokerProtectionService(self.db, self.broker)
                prot_svc.record_entry_protection(order_res, broker_pos, allow_watchdog_fallback=watchdog_ok)

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
        Bar close bracket watchdog and comprehensive exit policy execution:
        Audits all open positions against stop-loss, take-profit, time stops,
        trailing stops, and pre-earnings liquidation rules.
        Returns list of (ticker, exit_reason, exit_price).
        """
        executed = self.exit_policy.run_exit_policy(current_prices)

        self.last_watchdog_run_at = datetime.now(timezone.utc)
        self.db.set_system_control(
            "watchdog_heartbeat",
            self.last_watchdog_run_at.isoformat(),
            actor="RiskEngine",
            reason="Watchdog heartbeat execution",
        )
        return [(e["ticker"], e["exit_reason"], e["exit_price"]) for e in executed]

    def is_watchdog_healthy(self, as_of: Optional[datetime] = None) -> bool:
        """
        Verifies that the software watchdog has executed within the required cadence threshold.
        """
        if self.last_watchdog_run_at is None:
            ctrl = self.db.get_system_control("watchdog_heartbeat")
            if ctrl and ctrl.get("value"):
                try:
                    self.last_watchdog_run_at = datetime.fromisoformat(ctrl["value"])
                except Exception:
                    pass

        if self.last_watchdog_run_at is None:
            return False

        now = as_of or datetime.now(timezone.utc)
        if self.last_watchdog_run_at.tzinfo is None:
            self.last_watchdog_run_at = self.last_watchdog_run_at.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        elapsed = (now - self.last_watchdog_run_at).total_seconds()
        return elapsed <= self.watchdog_max_cadence_seconds

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
