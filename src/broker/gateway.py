"""
Execution Gateway for Paper Broker Adapters.
Enforces:
1. Strict paper-trading-only policy (ADR 0001).
2. Idempotent submission boundary via OrderIntent request hashing and UUID idempotency keys.
3. Classified retry & timeout outcomes: timeouts and network dropouts result in
   UNKNOWN_PENDING_RECONCILIATION, strictly prohibiting blind retries (ADR 0002).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.broker.base import AbstractBrokerAdapter
from src.domain.models import (
    AuditSeverity,
    BrokerSubmissionOutcome,
    OrderIntent,
    OrderIntentStatus,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
)
from src.storage.db import Database

logger = logging.getLogger(__name__)


class GatewayExecutionResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    intent_id: str
    idempotency_key: str
    outcome: BrokerSubmissionOutcome
    intent_status: OrderIntentStatus
    success: bool
    order_result: Optional[OrderResult] = None
    broker_order_id: Optional[str] = None
    error_message: Optional[str] = None
    requires_reconciliation: bool = False
    is_idempotent_duplicate: bool = False
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ExecutionGateway:
    def __init__(
        self,
        broker: AbstractBrokerAdapter,
        db: Database,
        mode: str = "paper",
    ):
        self.broker = broker
        self.db = db
        self.mode = mode.lower()
        self._enforce_paper_trading_policy()

    def _enforce_paper_trading_policy(self) -> None:
        """Enforces ADR 0001: Live execution is strictly prohibited."""
        if self.mode not in ("paper", "simulation", "backtest"):
            self.db.save_audit_log(
                severity=AuditSeverity.CRITICAL,
                component="ExecutionGateway",
                event_name="LIVE_TRADING_BLOCKED",
                message=f"Attempted execution in forbidden mode '{self.mode}'. Only paper/simulation allowed.",
            )
            raise RuntimeError(
                f"Execution mode '{self.mode}' is strictly forbidden by ADR 0001. "
                "Only 'paper', 'simulation', or 'backtest' execution is permitted."
            )

    def execute_intent(self, intent: OrderIntent) -> GatewayExecutionResult:
        """
        Executes an OrderIntent through the paper broker with idempotency and classified retry outcomes.
        1. Checks database for existing intent with matching idempotency key.
        2. If duplicate, returns existing state without re-executing against broker.
        3. If in UNKNOWN_PENDING_RECONCILIATION, blocks submission and flags reconciliation requirement.
        4. Submits to broker, classifying outcomes into ACCEPTED, REJECTED, or UNKNOWN_PENDING_RECONCILIATION.
        """
        self._enforce_paper_trading_policy()

        # Step 1: Check Idempotency Boundary
        existing = self.db.get_order_intent_by_idempotency_key(intent.idempotency_key)
        if existing:
            existing_status = OrderIntentStatus(existing["status"])

            # If already reconciled or previously submitted, return idempotent duplicate result
            if existing_status in (OrderIntentStatus.SUBMITTED, OrderIntentStatus.RECONCILED):
                subs = self.db.get_submissions_for_intent(existing["intent_id"])
                broker_order_id = subs[-1]["broker_order_id"] if subs else None
                return GatewayExecutionResult(
                    intent_id=existing["intent_id"],
                    idempotency_key=intent.idempotency_key,
                    outcome=BrokerSubmissionOutcome.ACCEPTED,
                    intent_status=existing_status,
                    success=True,
                    broker_order_id=broker_order_id,
                    is_idempotent_duplicate=True,
                    error_message=f"Duplicate submission with idempotency key '{intent.idempotency_key}' prevented by gateway",
                )

            # If unknown pending reconciliation, fail closed!
            if existing_status == OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION:
                return GatewayExecutionResult(
                    intent_id=existing["intent_id"],
                    idempotency_key=intent.idempotency_key,
                    outcome=BrokerSubmissionOutcome.UNKNOWN_PENDING_RECONCILIATION,
                    intent_status=existing_status,
                    success=False,
                    requires_reconciliation=True,
                    is_idempotent_duplicate=True,
                    error_message=f"OrderIntent {existing['intent_id']} is pending reconciliation; duplicate submission blocked (ADR 0002)",
                )

            if existing_status in (OrderIntentStatus.REJECTED, OrderIntentStatus.EXPIRED):
                return GatewayExecutionResult(
                    intent_id=existing["intent_id"],
                    idempotency_key=intent.idempotency_key,
                    outcome=BrokerSubmissionOutcome.REJECTED,
                    intent_status=existing_status,
                    success=False,
                    is_idempotent_duplicate=True,
                    error_message=f"OrderIntent previously finished with status {existing_status.value}",
                )

        # Step 2: Record fresh OrderIntent in database
        self.db.save_order_intent(
            intent_id=intent.intent_id,
            request_hash=intent.request_hash,
            ticker=intent.ticker,
            side=intent.side.value,
            target_qty=intent.target_qty,
            allocated_usd=intent.allocated_usd,
            idempotency_key=intent.idempotency_key,
            risk_decision=intent.risk_decision,
            candidate_id=intent.candidate_id,
            limit_price=intent.limit_price,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            risk_reason=intent.risk_reason,
            status=OrderIntentStatus.CREATED.value,
        )

        # Step 3: Construct OrderRequest for the Broker Adapter
        client_order_id = f"ord_{intent.ticker.lower()}_{intent.idempotency_key[:8]}"
        order_type = OrderType.BRACKET if (intent.stop_loss and intent.take_profit) else OrderType.MARKET

        order_req = OrderRequest(
            client_order_id=client_order_id,
            ticker=intent.ticker,
            side=intent.side,
            order_type=order_type,
            dollar_amount=intent.allocated_usd,
            target_qty=intent.target_qty,
            limit_price=intent.limit_price,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
        )

        redacted_request = json.dumps(
            {
                "client_order_id": client_order_id,
                "ticker": intent.ticker,
                "side": intent.side.value,
                "target_qty": intent.target_qty,
                "dollar_amount": intent.allocated_usd,
                "order_type": order_type.value,
            }
        )

        submission_id = f"sub_{uuid.uuid4().hex[:12]}"

        # Step 4: Submit to Broker Adapter with Exception Classification
        try:
            order_res = self.broker.submit_order(order_req)

            if order_res.status in (OrderState.ROUTED_TO_BROKER, OrderState.FILLED):
                # Successfully accepted by paper broker
                self.db.record_broker_submission(
                    submission_id=submission_id,
                    intent_id=intent.intent_id,
                    client_order_id=client_order_id,
                    broker_order_id=order_res.order_id,
                    attempt_number=1,
                    outcome_classification=BrokerSubmissionOutcome.ACCEPTED.value,
                    request_payload_redacted=redacted_request,
                    response_payload_redacted=json.dumps({"status": order_res.status.value, "broker_order_id": order_res.order_id}),
                )
                intent.transition_to(OrderIntentStatus.SUBMITTED)
                self.db.update_order_intent_status(intent.intent_id, OrderIntentStatus.SUBMITTED.value)

                return GatewayExecutionResult(
                    intent_id=intent.intent_id,
                    idempotency_key=intent.idempotency_key,
                    outcome=BrokerSubmissionOutcome.ACCEPTED,
                    intent_status=OrderIntentStatus.SUBMITTED,
                    success=True,
                    order_result=order_res,
                    broker_order_id=order_res.order_id,
                )

            else:
                # Rejected by broker adapter
                rejection_msg = order_res.error_message or "Rejected by broker"
                self.db.record_broker_submission(
                    submission_id=submission_id,
                    intent_id=intent.intent_id,
                    client_order_id=client_order_id,
                    broker_order_id=order_res.order_id,
                    attempt_number=1,
                    outcome_classification=BrokerSubmissionOutcome.REJECTED.value,
                    request_payload_redacted=redacted_request,
                    response_payload_redacted=json.dumps({"status": order_res.status.value, "reason": rejection_msg}),
                )
                intent.transition_to(OrderIntentStatus.REJECTED, reason=rejection_msg)
                self.db.update_order_intent_status(intent.intent_id, OrderIntentStatus.REJECTED.value)

                return GatewayExecutionResult(
                    intent_id=intent.intent_id,
                    idempotency_key=intent.idempotency_key,
                    outcome=BrokerSubmissionOutcome.REJECTED,
                    intent_status=OrderIntentStatus.REJECTED,
                    success=False,
                    order_result=order_res,
                    error_message=rejection_msg,
                )

        except TimeoutError as timeout_exc:
            # ADR 0002: Broker timeout produces UNKNOWN_PENDING_RECONCILIATION, NEVER a blind retry
            err_msg = f"Broker timeout during submission: {timeout_exc}"
            logger.error(err_msg)
            self.db.record_broker_submission(
                submission_id=submission_id,
                intent_id=intent.intent_id,
                client_order_id=client_order_id,
                attempt_number=1,
                outcome_classification=BrokerSubmissionOutcome.TIMEOUT.value,
                request_payload_redacted=redacted_request,
                response_payload_redacted=json.dumps({"error": "TimeoutError", "detail": str(timeout_exc)}),
            )
            intent.transition_to(OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION, reason=err_msg)
            self.db.update_order_intent_status(intent.intent_id, OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION.value)

            self.db.save_audit_log(
                severity=AuditSeverity.WARNING,
                component="ExecutionGateway",
                event_name="BROKER_TIMEOUT_UNKNOWN_STATUS",
                message=f"Order submission timed out for {intent.ticker}. Routed to UNKNOWN_PENDING_RECONCILIATION without blind retry.",
            )

            return GatewayExecutionResult(
                intent_id=intent.intent_id,
                idempotency_key=intent.idempotency_key,
                outcome=BrokerSubmissionOutcome.TIMEOUT,
                intent_status=OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION,
                success=False,
                error_message=err_msg,
                requires_reconciliation=True,
            )

        except Exception as exc:
            # Network drop or unexpected error
            err_msg = f"Network or adapter communication error: {exc}"
            logger.error(err_msg)
            self.db.record_broker_submission(
                submission_id=submission_id,
                intent_id=intent.intent_id,
                client_order_id=client_order_id,
                attempt_number=1,
                outcome_classification=BrokerSubmissionOutcome.UNKNOWN_PENDING_RECONCILIATION.value,
                request_payload_redacted=redacted_request,
                response_payload_redacted=json.dumps({"error": type(exc).__name__, "detail": str(exc)}),
            )
            intent.transition_to(OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION, reason=err_msg)
            self.db.update_order_intent_status(intent.intent_id, OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION.value)

            self.db.save_audit_log(
                severity=AuditSeverity.ERROR,
                component="ExecutionGateway",
                event_name="BROKER_COMMUNICATION_UNKNOWN_STATUS",
                message=f"Broker communication error for {intent.ticker}: {exc}. Placed in UNKNOWN_PENDING_RECONCILIATION.",
            )

            return GatewayExecutionResult(
                intent_id=intent.intent_id,
                idempotency_key=intent.idempotency_key,
                outcome=BrokerSubmissionOutcome.UNKNOWN_PENDING_RECONCILIATION,
                intent_status=OrderIntentStatus.UNKNOWN_PENDING_RECONCILIATION,
                success=False,
                error_message=err_msg,
                requires_reconciliation=True,
            )
