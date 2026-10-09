"""
Broker Protection Service (P3-01 / P3-02).
Manages and verifies native server-side bracket orders vs software watchdog protection.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.broker.base import AbstractBrokerAdapter
from src.domain.models import (
    AuditSeverity,
    OrderResult,
    Position,
    ProtectionMode,
    ProtectionStatusRecord,
)
from src.storage.db import Database

logger = logging.getLogger(__name__)


class BrokerProtectionService:
    def __init__(self, db: Database, broker: AbstractBrokerAdapter):
        self.db = db
        self.broker = broker

    def record_entry_protection(
        self,
        order_result: OrderResult,
        position: Position,
        allow_watchdog_fallback: bool = True,
    ) -> ProtectionStatusRecord:
        """
        Determines and records the protection mode for a newly filled entry.
        Identifies native broker bracket vs software watchdog vs degraded unprotected.
        """
        protection_id = f"prot_{uuid.uuid4().hex[:10]}"
        sl_order_id: Optional[str] = None
        tp_order_id: Optional[str] = None
        degradation_reason: Optional[str] = None

        raw = order_result.raw_response or {}
        if isinstance(raw, dict):
            sl_order_id = raw.get("stop_loss_order_id")
            tp_order_id = raw.get("take_profit_order_id")

            # Check legs array if present
            legs = raw.get("legs", [])
            if isinstance(legs, list):
                for leg in legs:
                    if isinstance(leg, dict):
                        leg_type = str(leg.get("type", "")).lower()
                        leg_id = leg.get("id") or leg.get("order_id")
                        if "stop" in leg_type and not sl_order_id:
                            sl_order_id = leg_id
                        elif "profit" in leg_type and not tp_order_id:
                            tp_order_id = leg_id

        if getattr(self.broker, "supports_native_bracket", False) and (sl_order_id or tp_order_id):
            mode = ProtectionMode.NATIVE_BRACKET
            watchdog_healthy = 1
        elif allow_watchdog_fallback:
            mode = ProtectionMode.WATCHDOG_SOFTWARE
            watchdog_healthy = 1
        else:
            mode = ProtectionMode.DEGRADED_UNPROTECTED
            watchdog_healthy = 0
            degradation_reason = "Broker lacks native bracket support and watchdog fallback is disabled"

        # Ensure position is in DB to satisfy foreign key constraint
        if position and not self.db.get_position(position.position_id):
            self.db.save_position(position)

        order_fk = None
        if order_result and order_result.order_id:
            if self.db.get_order(order_result.order_id):
                order_fk = order_result.order_id

        record = ProtectionStatusRecord(
            protection_id=protection_id,
            position_id=position.position_id,
            order_id=order_fk,
            ticker=position.ticker.upper(),
            protection_mode=mode,
            stop_loss_order_id=sl_order_id,
            take_profit_order_id=tp_order_id,
            watchdog_healthy=watchdog_healthy,
            last_verified_at=datetime.now(timezone.utc),
            degradation_reason=degradation_reason,
        )

        self.db.save_protection_status(
            protection_id=record.protection_id,
            position_id=record.position_id,
            order_id=record.order_id,
            ticker=record.ticker,
            protection_mode=record.protection_mode.value,
            stop_loss_order_id=record.stop_loss_order_id,
            take_profit_order_id=record.take_profit_order_id,
            watchdog_healthy=record.watchdog_healthy,
            last_verified_at=record.last_verified_at,
            degradation_reason=record.degradation_reason,
        )

        self.db.save_audit_log(
            severity=AuditSeverity.INFO if watchdog_healthy else AuditSeverity.WARNING,
            component="BrokerProtectionService",
            event_name="PROTECTION_RECORDED",
            message=f"Protection recorded for {record.ticker}: {record.protection_mode.value}",
            metadata={
                "protection_id": record.protection_id,
                "ticker": record.ticker,
                "mode": record.protection_mode.value,
                "sl_order_id": record.stop_loss_order_id,
                "tp_order_id": record.take_profit_order_id,
            },
        )
        return record

    def verify_native_legs(self, position: Position) -> Tuple[bool, Optional[str]]:
        """
        Verifies that recorded native bracket leg orders exist on the broker.
        If legs are missing or cancelled unexpectedly, marks status as DEGRADED_UNPROTECTED.
        """
        pos_id = position.position_id
        raw_status = self.db.get_protection_status_for_position(pos_id)
        if not raw_status:
            return False, f"No protection record found in DB for position {pos_id}"

        mode = ProtectionMode(raw_status["protection_mode"])
        if mode != ProtectionMode.NATIVE_BRACKET:
            return True, None

        sl_id = raw_status.get("stop_loss_order_id")
        tp_id = raw_status.get("take_profit_order_id")

        open_orders = self.broker.get_open_orders()
        # Collect broker order IDs
        open_ids = set()
        for o in open_orders:
            if isinstance(o, dict):
                open_ids.add(str(o.get("order_id") or o.get("id")))
            elif hasattr(o, "order_id"):
                open_ids.add(str(o.order_id))

        missing_legs = []
        if sl_id and str(sl_id) not in open_ids:
            missing_legs.append(f"stop_loss ({sl_id})")
        if tp_id and str(tp_id) not in open_ids:
            missing_legs.append(f"take_profit ({tp_id})")

        if missing_legs:
            reason = f"Native bracket legs missing on broker: {', '.join(missing_legs)}"
            self.db.save_protection_status(
                protection_id=raw_status["protection_id"],
                position_id=pos_id,
                order_id=raw_status.get("order_id"),
                ticker=position.ticker.upper(),
                protection_mode=ProtectionMode.DEGRADED_UNPROTECTED.value,
                stop_loss_order_id=sl_id,
                take_profit_order_id=tp_id,
                watchdog_healthy=0,
                last_verified_at=datetime.now(timezone.utc),
                degradation_reason=reason,
            )
            self.db.save_audit_log(
                severity=AuditSeverity.CRITICAL,
                component="BrokerProtectionService",
                event_name="NATIVE_LEGS_RECONCILIATION_FAILED",
                message=reason,
                metadata={"position_id": pos_id, "ticker": position.ticker, "missing": missing_legs},
            )
            return False, reason

        # Refresh verified timestamp
        self.db.save_protection_status(
            protection_id=raw_status["protection_id"],
            position_id=pos_id,
            order_id=raw_status.get("order_id"),
            ticker=position.ticker.upper(),
            protection_mode=ProtectionMode.NATIVE_BRACKET.value,
            stop_loss_order_id=sl_id,
            take_profit_order_id=tp_id,
            watchdog_healthy=1,
            last_verified_at=datetime.now(timezone.utc),
            degradation_reason=None,
        )
        return True, None

    def reconcile_protection(
        self, open_positions: List[Position]
    ) -> List[ProtectionStatusRecord]:
        """
        Reconciles protection records for all active open positions.
        """
        results: List[ProtectionStatusRecord] = []
        for pos in open_positions:
            raw = self.db.get_protection_status_for_position(pos.position_id)
            if not raw:
                # Untracked position -> mark degraded unprotected
                record = ProtectionStatusRecord(
                    protection_id=f"prot_{uuid.uuid4().hex[:10]}",
                    position_id=pos.position_id,
                    ticker=pos.ticker.upper(),
                    protection_mode=ProtectionMode.DEGRADED_UNPROTECTED,
                    watchdog_healthy=0,
                    last_verified_at=datetime.now(timezone.utc),
                    degradation_reason="Unrecorded open position during protection audit",
                )
                self.db.save_protection_status(
                    protection_id=record.protection_id,
                    position_id=record.position_id,
                    ticker=record.ticker,
                    protection_mode=record.protection_mode.value,
                    watchdog_healthy=0,
                    last_verified_at=record.last_verified_at,
                    degradation_reason=record.degradation_reason,
                )
                results.append(record)
            else:
                self.verify_native_legs(pos)
                updated_raw = self.db.get_protection_status_for_position(pos.position_id)
                if updated_raw:
                    results.append(
                        ProtectionStatusRecord(
                            protection_id=updated_raw["protection_id"],
                            position_id=updated_raw.get("position_id"),
                            order_id=updated_raw.get("order_id"),
                            ticker=updated_raw["ticker"],
                            protection_mode=ProtectionMode(updated_raw["protection_mode"]),
                            stop_loss_order_id=updated_raw.get("stop_loss_order_id"),
                            take_profit_order_id=updated_raw.get("take_profit_order_id"),
                            watchdog_healthy=updated_raw.get("watchdog_healthy", 1),
                            last_verified_at=datetime.fromisoformat(updated_raw["last_verified_at"]),
                            degradation_reason=updated_raw.get("degradation_reason"),
                        )
                    )
        return results
