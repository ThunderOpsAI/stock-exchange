"""
Broker Reconciliation Service (SPEC.md Phase 2, Ticket 12 / P2-04).
Implements broker state reconciliation on startup, before a trading run, and after order submission.

Performs bidirectional comparison between local SQLite domain storage and broker adapter:
1. Orders: broker open orders vs local open/pending orders and order intents.
2. Positions: broker positions vs local open positions.
3. Fills: partial fills and fill status synchronization.
4. Orphaned positions: broker holdings without corresponding local records.
5. Unknown recovery: resolves UNKNOWN_PENDING_RECONCILIATION intents into RECONCILED or REJECTED.
6. Cryptographic lineage: SHA-256 hashes of local and broker snapshots recorded in reconciliation_events.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field

from src.broker.base import AbstractBrokerAdapter
from src.domain.models import (
    AuditSeverity,
    ExitReason,
    Fill,
    Order,
    OrderIntent,
    OrderIntentStatus,
    OrderSide,
    OrderState,
    OrderType,
    Position,
    PositionStatus,
    ReconciliationRecord,
    ReconciliationStatus,
)
from src.storage.db import Database

logger = logging.getLogger(__name__)


class ReconciliationDiscrepancyType(str, Enum):
    MISSING_LOCAL_ORDER = "MISSING_LOCAL_ORDER"
    MISSING_BROKER_ORDER = "MISSING_BROKER_ORDER"
    PARTIAL_FILL = "PARTIAL_FILL"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    ORPHANED_BROKER_POSITION = "ORPHANED_BROKER_POSITION"


class ReconciliationDiscrepancy(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    discrepancy_type: ReconciliationDiscrepancyType
    entity_type: str  # "order", "position", "intent", "fill"
    entity_id: Optional[str] = None
    ticker: Optional[str] = None
    local_value: Optional[Any] = None
    broker_value: Optional[Any] = None
    description: str
    is_critical: bool = True


class ReconciliationResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    event_id: str
    run_id: Optional[str] = None
    status: ReconciliationStatus
    is_clean: bool
    requires_intervention: bool
    discrepancies: List[ReconciliationDiscrepancy] = Field(default_factory=list)
    local_snapshot_hash: str
    broker_snapshot_hash: str
    resolved_intents: List[str] = Field(default_factory=list)
    notes: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReconciliationService:
    """
    Reconciliation Engine for validating broker and local state consistency.
    Executes bidirectional verification, identifies orphaned positions and missing orders,
    resolves ambiguous pending-reconciliation intents, and logs tamper-evident snapshot hashes.
    """

    def __init__(self, db: Database, broker: Any):
        self.db = db
        self.broker = broker

    def reconcile(self, run_id: Optional[str] = None) -> ReconciliationResult:
        """
        Executes a complete reconciliation pass:
        1. Fetches local snapshot (open positions, open orders, pending order intents).
        2. Fetches broker snapshot (positions, open orders).
        3. Computes deterministic SHA-256 cryptographic hashes for both snapshots.
        4. Resolves any UNKNOWN_PENDING_RECONCILIATION order intents.
        5. Performs bidirectional discrepancy checks across positions, orders, and fills.
        6. Audits and records the event in `reconciliation_events`.
        """
        event_id = f"rec_{uuid.uuid4().hex[:12]}"

        # Step 1: Fetch local state snapshot
        local_positions = self.db.get_open_positions()
        local_orders = self.db.get_open_orders()
        local_intents = self.db.get_pending_order_intents()

        # Step 2: Fetch broker state snapshot
        broker_positions = self._get_broker_positions()
        raw_broker_orders = self._get_broker_open_orders()
        normalized_broker_orders = [self._normalize_broker_order(bo) for bo in raw_broker_orders]

        # Step 3: Compute cryptographic snapshot hashes
        local_hash, broker_hash = self._compute_snapshot_hashes(
            local_positions=local_positions,
            local_orders=local_orders,
            local_intents=local_intents,
            broker_positions=broker_positions,
            normalized_broker_orders=normalized_broker_orders,
        )

        discrepancies: List[ReconciliationDiscrepancy] = []
        resolved_intents: List[str] = []

        # Step 4: Handle and resolve UNKNOWN_PENDING_RECONCILIATION intents
        unknown_intents = [
            i for i in local_intents if i.get("status") == OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION.value
        ]
        if unknown_intents:
            res_intents, unres_discrepancies = self._resolve_unknown_intents(
                unknown_intents=unknown_intents,
                broker_positions=broker_positions,
                broker_orders=normalized_broker_orders,
            )
            resolved_intents.extend(res_intents)
            discrepancies.extend(unres_discrepancies)

            # Refresh snapshots if state changed during resolution
            if resolved_intents:
                local_positions = self.db.get_open_positions()
                local_orders = self.db.get_open_orders()
                local_intents = self.db.get_pending_order_intents()
                broker_positions = self._get_broker_positions()

        # Step 5: Bidirectional Comparison
        # 5A: Positions Comparison
        pos_discrepancies = self._compare_positions(local_positions, broker_positions)
        discrepancies.extend(pos_discrepancies)

        # 5B: Orders & Fills Comparison
        order_discrepancies = self._compare_orders_and_fills(
            local_orders=local_orders,
            local_intents=local_intents,
            broker_orders=normalized_broker_orders,
        )
        discrepancies.extend(order_discrepancies)

        # Step 6: Classify outcome status
        if discrepancies:
            status = ReconciliationStatus.UNRESOLVED_DISCREPANCY
            is_clean = False
            requires_intervention = True
            notes = f"Reconciliation detected {len(discrepancies)} discrepancy(ies)."
        elif resolved_intents:
            status = ReconciliationStatus.RESOLVED_RECONCILED
            is_clean = True
            requires_intervention = False
            notes = f"Reconciliation resolved {len(resolved_intents)} pending intent(s) to clean state."
        else:
            status = ReconciliationStatus.HEALTHY_MATCH
            is_clean = True
            requires_intervention = False
            notes = "Reconciliation clean: Local DB and broker state are in perfect agreement."

        # Step 7: Record event in database
        mismatches_payload = [d.model_dump() for d in discrepancies]
        mismatches_json = json.dumps(mismatches_payload)

        # Enforce foreign key safety for run_id if provided
        valid_run_id = run_id
        if run_id and not self.db.get_trading_run(run_id):
            valid_run_id = None

        self.db.record_reconciliation_event(
            event_id=event_id,
            run_id=valid_run_id,
            local_snapshot_hash=local_hash,
            broker_snapshot_hash=broker_hash,
            mismatches_json=mismatches_json,
            resolution_status=status.value,
            resolution_notes=notes,
        )

        # Step 8: Log audit trail
        severity = AuditSeverity.WARNING if not is_clean else AuditSeverity.INFO
        self.db.save_audit_log(
            severity=severity,
            component="ReconciliationService",
            event_name="RECONCILIATION_RUN_COMPLETED",
            message=notes,
            metadata={
                "event_id": event_id,
                "run_id": run_id,
                "status": status.value,
                "is_clean": is_clean,
                "discrepancies_count": len(discrepancies),
                "resolved_intents_count": len(resolved_intents),
            },
        )

        return ReconciliationResult(
            event_id=event_id,
            run_id=run_id,
            status=status,
            is_clean=is_clean,
            requires_intervention=requires_intervention,
            discrepancies=discrepancies,
            local_snapshot_hash=local_hash,
            broker_snapshot_hash=broker_hash,
            resolved_intents=resolved_intents,
            notes=notes,
        )

    # -------------------------------------------------------------
    # Helper Inspection & Fetch Methods
    # -------------------------------------------------------------

    def _get_broker_positions(self) -> List[Position]:
        if hasattr(self.broker, "get_positions") and callable(self.broker.get_positions):
            return self.broker.get_positions()
        return []

    def _get_broker_open_orders(self) -> List[Any]:
        if hasattr(self.broker, "get_open_orders") and callable(self.broker.get_open_orders):
            return self.broker.get_open_orders()
        if hasattr(self.broker, "pending_orders"):
            return list(self.broker.pending_orders.values())
        return []

    def _normalize_broker_order(self, bo: Any) -> Dict[str, Any]:
        """Extracts uniform fields from varied broker order representations."""
        if isinstance(bo, dict):
            return {
                "order_id": str(bo.get("order_id") or bo.get("id") or ""),
                "client_order_id": str(bo.get("client_order_id") or ""),
                "ticker": str(bo.get("ticker") or bo.get("symbol") or "").upper(),
                "side": str(bo.get("side", "")).upper(),
                "qty": float(bo.get("target_qty") or bo.get("qty") or 0.0),
                "filled_qty": float(bo.get("filled_qty", 0.0)),
                "status": str(bo.get("status") or bo.get("state") or "").upper(),
                "filled_price": float(bo.get("filled_price", 0.0)) if bo.get("filled_price") else None,
                "raw": bo,
            }

        order_id = getattr(bo, "order_id", getattr(bo, "id", None))
        client_order_id = getattr(bo, "client_order_id", None)
        ticker = getattr(bo, "ticker", getattr(bo, "symbol", ""))
        side = getattr(bo, "side", "")
        if hasattr(side, "value"):
            side = side.value
        qty = getattr(bo, "target_qty", getattr(bo, "qty", 0.0))
        filled_qty = getattr(bo, "filled_qty", 0.0)
        status = getattr(bo, "state", getattr(bo, "status", ""))
        if hasattr(status, "value"):
            status = status.value
        filled_price = getattr(bo, "filled_price", None)

        return {
            "order_id": str(order_id) if order_id is not None else "",
            "client_order_id": str(client_order_id) if client_order_id is not None else "",
            "ticker": str(ticker).upper(),
            "side": str(side).upper(),
            "qty": float(qty) if qty is not None else 0.0,
            "filled_qty": float(filled_qty) if filled_qty is not None else 0.0,
            "status": str(status).upper(),
            "filled_price": float(filled_price) if filled_price is not None else None,
            "raw": bo,
        }

    # -------------------------------------------------------------
    # Cryptographic Lineage Tracking
    # -------------------------------------------------------------

    def _compute_snapshot_hashes(
        self,
        local_positions: List[Position],
        local_orders: List[Order],
        local_intents: List[Dict[str, Any]],
        broker_positions: List[Position],
        normalized_broker_orders: List[Dict[str, Any]],
    ) -> Tuple[str, str]:
        """Computes deterministic SHA-256 hashes of local and broker snapshots."""
        local_data = {
            "positions": sorted(
                [
                    {
                        "ticker": p.ticker.upper(),
                        "side": p.side.value if hasattr(p.side, "value") else str(p.side),
                        "qty": round(float(p.qty), 4),
                        "entry_price": round(float(p.entry_price), 2),
                        "position_id": p.position_id,
                    }
                    for p in local_positions
                ],
                key=lambda x: (x["ticker"], x["position_id"]),
            ),
            "orders": sorted(
                [
                    {
                        "order_id": o.order_id,
                        "client_order_id": o.client_order_id,
                        "ticker": o.ticker.upper(),
                        "side": o.side.value if hasattr(o.side, "value") else str(o.side),
                        "state": o.state.value if hasattr(o.state, "value") else str(o.state),
                        "target_qty": round(float(o.target_qty), 4),
                    }
                    for o in local_orders
                ],
                key=lambda x: x["order_id"],
            ),
            "intents": sorted(
                [
                    {
                        "intent_id": i["intent_id"],
                        "ticker": i["ticker"].upper(),
                        "side": i["side"],
                        "status": i["status"],
                        "target_qty": round(float(i["target_qty"]), 4),
                        "idempotency_key": i["idempotency_key"],
                    }
                    for i in local_intents
                ],
                key=lambda x: x["intent_id"],
            ),
        }
        local_canonical = json.dumps(local_data, sort_keys=True)
        local_hash = hashlib.sha256(local_canonical.encode("utf-8")).hexdigest()

        broker_data = {
            "positions": sorted(
                [
                    {
                        "ticker": p.ticker.upper(),
                        "side": p.side.value if hasattr(p.side, "value") else str(p.side),
                        "qty": round(float(p.qty), 4),
                        "entry_price": round(float(p.entry_price), 2),
                        "position_id": getattr(p, "position_id", ""),
                    }
                    for p in broker_positions
                ],
                key=lambda x: (x["ticker"], x["position_id"]),
            ),
            "orders": sorted(
                [
                    {
                        "order_id": o["order_id"],
                        "client_order_id": o["client_order_id"],
                        "ticker": o["ticker"],
                        "side": o["side"],
                        "status": o["status"],
                        "qty": round(o["qty"], 4),
                        "filled_qty": round(o["filled_qty"], 4),
                    }
                    for o in normalized_broker_orders
                ],
                key=lambda x: (x["order_id"], x["client_order_id"]),
            ),
        }
        broker_canonical = json.dumps(broker_data, sort_keys=True)
        broker_hash = hashlib.sha256(broker_canonical.encode("utf-8")).hexdigest()

        return local_hash, broker_hash

    # -------------------------------------------------------------
    # Resolving Ambiguous / Unknown Order Intents
    # -------------------------------------------------------------

    def _resolve_unknown_intents(
        self,
        unknown_intents: List[Dict[str, Any]],
        broker_positions: List[Position],
        broker_orders: List[Dict[str, Any]],
    ) -> Tuple[List[str], List[ReconciliationDiscrepancy]]:
        """
        Resolves UNKNOWN_PENDING_RECONCILIATION intents according to ADR 0002.
        - If broker confirmed filled or held in broker position -> transition to RECONCILED and sync DB.
        - If broker confirmed cancelled/rejected or verified absent -> transition to REJECTED.
        """
        resolved: List[str] = []
        unresolved_discrepancies: List[ReconciliationDiscrepancy] = []

        broker_orders_by_id = {bo["order_id"]: bo for bo in broker_orders if bo["order_id"]}
        broker_orders_by_client_id = {bo["client_order_id"]: bo for bo in broker_orders if bo["client_order_id"]}
        broker_positions_by_ticker = {bp.ticker.upper(): bp for bp in broker_positions}

        for intent in unknown_intents:
            intent_id = intent["intent_id"]
            ticker = intent["ticker"].upper()
            subs = self.db.get_submissions_for_intent(intent_id)

            broker_order_id = subs[-1].get("broker_order_id") if subs else None
            client_order_id = subs[-1].get("client_order_id") if subs else f"ord_{ticker.lower()}_{intent['idempotency_key'][:8]}"

            # Check if broker has explicit query API (e.g. broker.get_order)
            broker_order_info: Optional[Dict[str, Any]] = None
            if broker_order_id and broker_order_id in broker_orders_by_id:
                broker_order_info = broker_orders_by_id[broker_order_id]
            elif client_order_id and client_order_id in broker_orders_by_client_id:
                broker_order_info = broker_orders_by_client_id[client_order_id]
            elif hasattr(self.broker, "get_order") and callable(self.broker.get_order):
                try:
                    res = self.broker.get_order(broker_order_id or client_order_id)
                    if res:
                        broker_order_info = self._normalize_broker_order(res)
                except Exception as exc:
                    logger.debug("Failed querying broker for order: %s", exc)

            # Case A: Broker confirms FILLED (or order was filled and position exists)
            is_filled = False
            fill_price = 0.0
            if broker_order_info and broker_order_info["status"] in ("FILLED", "OrderState.FILLED"):
                is_filled = True
                fill_price = broker_order_info.get("filled_price") or 0.0
            elif ticker in broker_positions_by_ticker:
                # Broker holds position matching this intent that is not present in local DB
                b_pos = broker_positions_by_ticker[ticker]
                local_pos = self.db.get_position_by_ticker(ticker)
                if not local_pos or local_pos.status != PositionStatus.OPEN:
                    is_filled = True
                    fill_price = b_pos.entry_price

            if is_filled:
                if fill_price <= 0.0:
                    fill_price = (
                        intent["allocated_usd"] / intent["target_qty"]
                        if intent["target_qty"] > 0
                        else 100.0
                    )

                # 1. Update intent status to RECONCILED
                self.db.update_order_intent_status(
                    intent_id=intent_id,
                    status=OrderIntentStatus.RECONCILED.value,
                    risk_reason="Reconciled: Broker confirmed order execution (FILLED)",
                )

                # 2. Sync local Order record
                local_order = self.db.get_order_by_client_id(client_order_id)
                if not local_order and broker_order_id:
                    local_order = self.db.get_order(broker_order_id)

                if local_order:
                    self.db.update_order_state(local_order.order_id, OrderState.FILLED)
                    order_ref_id = local_order.order_id
                else:
                    order_ref_id = broker_order_id or f"ord_{intent_id}"
                    self.db.save_order(
                        Order(
                            order_id=order_ref_id,
                            client_order_id=client_order_id,
                            candidate_id=intent.get("candidate_id"),
                            ticker=ticker,
                            side=OrderSide(intent["side"]),
                            order_type=OrderType.MARKET,
                            allocated_usd=intent["allocated_usd"],
                            target_qty=intent["target_qty"],
                            stop_loss=intent.get("stop_loss"),
                            take_profit=intent.get("take_profit"),
                            state=OrderState.FILLED,
                        )
                    )

                # 3. Record Fill in DB
                existing_fills = self.db.get_fills_for_order(order_ref_id)
                if not existing_fills:
                    self.db.save_fill(
                        Fill(
                            fill_id=f"fill_{uuid.uuid4().hex[:8]}",
                            order_id=order_ref_id,
                            broker_order_id=broker_order_id,
                            ticker=ticker,
                            side=OrderSide(intent["side"]),
                            filled_qty=intent["target_qty"],
                            filled_price=fill_price,
                            filled_notional=round(intent["target_qty"] * fill_price, 2),
                            executed_at=datetime.now(timezone.utc),
                        )
                    )

                # 4. Sync Position in DB
                local_pos = self.db.get_position_by_ticker(ticker)
                if not local_pos or local_pos.status != PositionStatus.OPEN:
                    pos_id = f"pos_{uuid.uuid4().hex[:8]}"
                    notional = round(intent["target_qty"] * fill_price, 2)
                    self.db.save_position(
                        Position(
                            position_id=pos_id,
                            broker_position_id=f"broker_{pos_id}",
                            ticker=ticker,
                            side=OrderSide(intent["side"]),
                            qty=intent["target_qty"],
                            entry_price=fill_price,
                            current_price=fill_price,
                            stop_loss=intent.get("stop_loss") or (fill_price * 0.95),
                            take_profit=intent.get("take_profit") or (fill_price * 1.10),
                            market_value=notional,
                            unrealized_pnl=0.0,
                            status=PositionStatus.OPEN,
                            opened_at=datetime.now(timezone.utc),
                        )
                    )

                resolved.append(intent_id)
                continue

            # Case B: Broker confirms REJECTED, CANCELLED, or ABSENT
            is_rejected_or_absent = False
            if broker_order_info and broker_order_info["status"] in (
                "CANCELLED",
                "REJECTED",
                "REJECTED_BY_BROKER",
                "EXPIRED",
            ):
                is_rejected_or_absent = True
            elif (
                not broker_order_info
                and ticker not in broker_positions_by_ticker
                and not any(bo["ticker"] == ticker for bo in broker_orders)
            ):
                # Broker has no record of the order and holds no position
                is_rejected_or_absent = True

            if is_rejected_or_absent:
                # 1. Update intent status to REJECTED
                self.db.update_order_intent_status(
                    intent_id=intent_id,
                    status=OrderIntentStatus.REJECTED.value,
                    risk_reason="Reconciled: Broker confirmed rejected, cancelled, or absent without execution",
                )

                # 2. Update local order state if exists
                local_order = self.db.get_order_by_client_id(client_order_id)
                if not local_order and broker_order_id:
                    local_order = self.db.get_order(broker_order_id)
                if local_order:
                    self.db.update_order_state(
                        local_order.order_id,
                        OrderState.REJECTED_BY_BROKER,
                        rejection_reason="Reconciled: Confirmed rejected or absent by broker",
                    )

                resolved.append(intent_id)
                continue

            # If still ambiguous and couldn't resolve
            unresolved_discrepancies.append(
                ReconciliationDiscrepancy(
                    discrepancy_type=ReconciliationDiscrepancyType.MISSING_BROKER_ORDER,
                    entity_type="intent",
                    entity_id=intent_id,
                    ticker=ticker,
                    local_value={"status": intent["status"], "idempotency_key": intent["idempotency_key"]},
                    broker_value=None,
                    description=f"OrderIntent {intent_id} is in UNKNOWN_PENDING_RECONCILIATION and could not be verified on broker.",
                    is_critical=True,
                )
            )

        return resolved, unresolved_discrepancies

    # -------------------------------------------------------------
    # Comparison Logic: Positions
    # -------------------------------------------------------------

    def _compare_positions(
        self,
        local_positions: List[Position],
        broker_positions: List[Position],
    ) -> List[ReconciliationDiscrepancy]:
        """Compares open positions between local DB and broker."""
        discrepancies: List[ReconciliationDiscrepancy] = []

        local_map: Dict[str, Position] = {
            p.ticker.upper(): p for p in local_positions if p.status == PositionStatus.OPEN and p.qty > 0
        }
        broker_map: Dict[str, Position] = {
            p.ticker.upper(): p
            for p in broker_positions
            if getattr(p, "status", PositionStatus.OPEN) == PositionStatus.OPEN and p.qty > 0
        }

        # Check for Orphaned Broker Positions and Quantity/Side Mismatches
        for ticker, b_pos in broker_map.items():
            if ticker not in local_map:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type=ReconciliationDiscrepancyType.ORPHANED_BROKER_POSITION,
                        entity_type="position",
                        entity_id=getattr(b_pos, "position_id", None) or getattr(b_pos, "broker_position_id", None),
                        ticker=ticker,
                        local_value=None,
                        broker_value={
                            "qty": b_pos.qty,
                            "side": b_pos.side.value if hasattr(b_pos.side, "value") else str(b_pos.side),
                            "entry_price": b_pos.entry_price,
                        },
                        description=(
                            f"Broker holds orphaned position for {ticker} (qty={b_pos.qty}) "
                            "with zero matching open position in local database."
                        ),
                        is_critical=True,
                    )
                )
            else:
                l_pos = local_map[ticker]
                l_side = l_pos.side.value if hasattr(l_pos.side, "value") else str(l_pos.side)
                b_side = b_pos.side.value if hasattr(b_pos.side, "value") else str(b_pos.side)
                qty_diff = abs(round(l_pos.qty, 4) - round(b_pos.qty, 4))

                if qty_diff > 1e-4 or l_side.upper() != b_side.upper():
                    discrepancies.append(
                        ReconciliationDiscrepancy(
                            discrepancy_type=ReconciliationDiscrepancyType.POSITION_MISMATCH,
                            entity_type="position",
                            entity_id=l_pos.position_id,
                            ticker=ticker,
                            local_value={"qty": l_pos.qty, "side": l_side},
                            broker_value={"qty": b_pos.qty, "side": b_side},
                            description=(
                                f"Position mismatch for {ticker}: local has {l_side} {l_pos.qty}, "
                                f"broker reports {b_side} {b_pos.qty}."
                            ),
                            is_critical=True,
                        )
                    )

        # Check for positions local thinks it holds, but broker does not
        for ticker, l_pos in local_map.items():
            if ticker not in broker_map:
                l_side = l_pos.side.value if hasattr(l_pos.side, "value") else str(l_pos.side)
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type=ReconciliationDiscrepancyType.POSITION_MISMATCH,
                        entity_type="position",
                        entity_id=l_pos.position_id,
                        ticker=ticker,
                        local_value={"qty": l_pos.qty, "side": l_side},
                        broker_value=None,
                        description=(
                            f"Local DB holds open position for {ticker} ({l_pos.qty} shares) "
                            "but broker holds no active position."
                        ),
                        is_critical=True,
                    )
                )

        return discrepancies

    # -------------------------------------------------------------
    # Comparison Logic: Orders & Fills
    # -------------------------------------------------------------

    def _compare_orders_and_fills(
        self,
        local_orders: List[Order],
        local_intents: List[Dict[str, Any]],
        broker_orders: List[Dict[str, Any]],
    ) -> List[ReconciliationDiscrepancy]:
        """Compares open orders and detected fills between local DB and broker."""
        discrepancies: List[ReconciliationDiscrepancy] = []

        local_order_ids: Set[str] = {o.order_id for o in local_orders if o.order_id}
        local_client_order_ids: Set[str] = {o.client_order_id for o in local_orders if o.client_order_id}

        # Gather known broker order IDs from broker_submissions
        for intent in local_intents:
            subs = self.db.get_submissions_for_intent(intent["intent_id"])
            for s in subs:
                if s.get("broker_order_id"):
                    local_order_ids.add(s["broker_order_id"])
                if s.get("client_order_id"):
                    local_client_order_ids.add(s["client_order_id"])

        matched_broker_order_ids: Set[str] = set()

        # 1. Inspect Broker Open Orders
        for bo in broker_orders:
            b_order_id = bo["order_id"]
            b_client_id = bo["client_order_id"]

            matched = (b_order_id and b_order_id in local_order_ids) or (
                b_client_id and b_client_id in local_client_order_ids
            )

            if not matched:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type=ReconciliationDiscrepancyType.MISSING_LOCAL_ORDER,
                        entity_type="order",
                        entity_id=b_order_id or b_client_id,
                        ticker=bo["ticker"],
                        local_value=None,
                        broker_value={
                            "order_id": b_order_id,
                            "client_order_id": b_client_id,
                            "side": bo["side"],
                            "qty": bo["qty"],
                            "status": bo["status"],
                        },
                        description=(
                            f"Broker has open order {b_order_id or b_client_id} for {bo['ticker']} "
                            "that does not exist in local database."
                        ),
                        is_critical=True,
                    )
                )
            else:
                if b_order_id:
                    matched_broker_order_ids.add(b_order_id)
                if b_client_id:
                    matched_broker_order_ids.add(b_client_id)

                # Check for Partial Fill discrepancies
                if bo["status"] == "PARTIALLY_FILLED" or (0 < bo["filled_qty"] < bo["qty"]):
                    # Find matching local order and fills
                    local_ord = self.db.get_order_by_client_id(b_client_id)
                    if not local_ord and b_order_id:
                        local_ord = self.db.get_order(b_order_id)

                    local_fills = self.db.get_fills_for_order(local_ord.order_id) if local_ord else []
                    local_filled_qty = sum(f.filled_qty for f in local_fills)

                    if abs(local_filled_qty - bo["filled_qty"]) > 1e-4:
                        discrepancies.append(
                            ReconciliationDiscrepancy(
                                discrepancy_type=ReconciliationDiscrepancyType.PARTIAL_FILL,
                                entity_type="order",
                                entity_id=b_order_id or (local_ord.order_id if local_ord else None),
                                ticker=bo["ticker"],
                                local_value={"filled_qty": local_filled_qty},
                                broker_value={"filled_qty": bo["filled_qty"], "target_qty": bo["qty"]},
                                description=(
                                    f"Partial fill discrepancy for {bo['ticker']}: broker reports filled "
                                    f"{bo['filled_qty']}/{bo['qty']}, but local records reflect {local_filled_qty}."
                                ),
                                is_critical=True,
                            )
                        )

        # 2. Inspect Local Open Orders
        for lo in local_orders:
            if lo.state in (OrderState.ROUTED_TO_BROKER, OrderState.PARTIALLY_FILLED):
                is_on_broker = (lo.order_id in matched_broker_order_ids) or (
                    lo.client_order_id in matched_broker_order_ids
                )
                if not is_on_broker:
                    # Order is pending locally but broker does not hold it open
                    discrepancies.append(
                        ReconciliationDiscrepancy(
                            discrepancy_type=ReconciliationDiscrepancyType.MISSING_BROKER_ORDER,
                            entity_type="order",
                            entity_id=lo.order_id,
                            ticker=lo.ticker,
                            local_value={"client_order_id": lo.client_order_id, "state": lo.state.value},
                            broker_value=None,
                            description=(
                                f"Local order {lo.order_id} ({lo.client_order_id}) is marked {lo.state.value} "
                                "but is missing from broker open orders."
                            ),
                            is_critical=True,
                        )
                    )

        # 3. Inspect Local Submitted Intents
        for intent in local_intents:
            if intent.get("status") == OrderIntentStatus.SUBMITTED.value:
                subs = self.db.get_submissions_for_intent(intent["intent_id"])
                has_broker_match = False
                for s in subs:
                    b_id = s.get("broker_order_id")
                    c_id = s.get("client_order_id")
                    if (b_id and b_id in matched_broker_order_ids) or (c_id and c_id in matched_broker_order_ids):
                        has_broker_match = True
                        break

                if not has_broker_match:
                    discrepancies.append(
                        ReconciliationDiscrepancy(
                            discrepancy_type=ReconciliationDiscrepancyType.MISSING_BROKER_ORDER,
                            entity_type="intent",
                            entity_id=intent["intent_id"],
                            ticker=intent["ticker"],
                            local_value={"status": intent["status"], "idempotency_key": intent["idempotency_key"]},
                            broker_value=None,
                            description=(
                                f"OrderIntent {intent['intent_id']} is marked SUBMITTED but has no "
                                "matching active order on the broker."
                            ),
                            is_critical=True,
                        )
                    )

        return discrepancies


# Engine alias for flexibility
ReconciliationEngine = ReconciliationService
