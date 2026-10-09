"""
Simulated Paper Broker Adapter for Local Testing, Backtesting, and Offline Simulation.
Implements fractional share execution, bracket tracking, and balance updates for a $100 sandbox.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.broker.base import AbstractBrokerAdapter
from src.domain.models import (
    AccountBalance,
    ExitReason,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
    Position,
    PositionStatus,
)


class SimulatedPaperBroker(AbstractBrokerAdapter):
    def __init__(
        self,
        initial_cash: float = 100.0,
        slippage_bps: float = 2.0,
        fee_per_trade: float = 0.0,
    ):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.realized_pnl = 0.0
        self.slippage_bps = slippage_bps
        self.fee_per_trade = fee_per_trade
        self.connected = False

        self.positions: Dict[str, Position] = {}  # position_id -> Position
        self.pending_orders: Dict[str, OrderRequest] = {}  # order_id -> OrderRequest
        self.open_orders: Dict[str, Any] = {}  # order_id -> simulated open Order
        self.price_feed: Dict[str, float] = {}  # ticker -> current_price

    @property
    def supports_native_bracket(self) -> bool:
        return True

    def get_open_orders(self) -> List[Any]:
        return list(self.open_orders.values())

    def connect(self) -> bool:
        self.connected = True
        return True

    def health_check(self) -> bool:
        return self.connected

    def set_price(self, ticker: str, price: float) -> None:
        """Helper to inject or update simulated price for testing."""
        self.price_feed[ticker.upper()] = price
        # Update existing position valuations
        for pos_id, pos in list(self.positions.items()):
            if pos.ticker == ticker.upper() and pos.status == PositionStatus.OPEN:
                mkt_val = round(pos.qty * price, 2)
                unrealized = round((price - pos.entry_price) * pos.qty, 2)
                updated = Position(
                    position_id=pos.position_id,
                    broker_position_id=pos.broker_position_id,
                    ticker=pos.ticker,
                    side=pos.side,
                    qty=pos.qty,
                    entry_price=pos.entry_price,
                    current_price=price,
                    stop_loss=pos.stop_loss,
                    take_profit=pos.take_profit,
                    market_value=mkt_val,
                    unrealized_pnl=unrealized,
                    status=PositionStatus.OPEN,
                    opened_at=pos.opened_at,
                )
                self.positions[pos_id] = updated

    def get_account_balance(self) -> AccountBalance:
        unrealized = sum(p.unrealized_pnl for p in self.positions.values() if p.status == PositionStatus.OPEN)
        invested = sum(p.market_value for p in self.positions.values() if p.status == PositionStatus.OPEN)
        equity = round(self.cash + invested, 2)
        return AccountBalance(
            cash=round(self.cash, 2),
            equity=equity,
            buying_power=round(self.cash, 2),
            currency="USD",
            unrealized_pnl=round(unrealized, 2),
            realized_pnl=round(self.realized_pnl, 2),
            updated_at=datetime.now(timezone.utc),
        )

    def get_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if p.status == PositionStatus.OPEN]

    def get_position(self, ticker: str) -> Optional[Position]:
        ticker_upper = ticker.upper()
        for p in self.positions.values():
            if p.ticker == ticker_upper and p.status == PositionStatus.OPEN:
                return p
        return None

    def submit_order(self, order: OrderRequest) -> OrderResult:
        if not self.connected:
            self.connect()

        ticker = order.ticker.upper()
        current_price = self.price_feed.get(ticker, 100.0)

        # Slippage calculation
        slippage_factor = 1.0 + (self.slippage_bps / 10000.0 if order.side == OrderSide.BUY else -self.slippage_bps / 10000.0)
        execution_price = round(current_price * slippage_factor, 2)

        order_id = f"sim_ord_{uuid.uuid4().hex[:8]}"

        if order.side == OrderSide.BUY:
            if order.dollar_amount > self.cash:
                return OrderResult(
                    order_id=order_id,
                    client_order_id=order.client_order_id,
                    ticker=ticker,
                    status=OrderState.REJECTED_BY_BROKER,
                    error_message=f"Insufficient cash: requested ${order.dollar_amount:.2f}, available ${self.cash:.2f}",
                )

            # Precision: 4 decimal places floored
            qty = math.floor((order.dollar_amount / execution_price) * 10000) / 10000.0
            if qty <= 0.0:
                return OrderResult(
                    order_id=order_id,
                    client_order_id=order.client_order_id,
                    ticker=ticker,
                    status=OrderState.REJECTED_BY_BROKER,
                    error_message="Calculated quantity is zero",
                )

            notional = round(qty * execution_price, 2)
            self.cash -= notional
            self.cash -= self.fee_per_trade

            pos_id = f"pos_{uuid.uuid4().hex[:8]}"
            stop_price = order.stop_loss or (execution_price * 0.95)
            take_price = order.take_profit or (execution_price * 1.10)

            legs = []
            sl_leg_id = None
            tp_leg_id = None
            if order.stop_loss:
                sl_leg_id = f"sim_leg_sl_{uuid.uuid4().hex[:8]}"
                self.open_orders[sl_leg_id] = {
                    "order_id": sl_leg_id,
                    "ticker": ticker,
                    "side": "SELL",
                    "type": "STOP_LOSS",
                    "stop_price": order.stop_loss,
                    "qty": qty,
                }
                legs.append({"id": sl_leg_id, "type": "stop_loss", "stop_price": order.stop_loss})

            if order.take_profit:
                tp_leg_id = f"sim_leg_tp_{uuid.uuid4().hex[:8]}"
                self.open_orders[tp_leg_id] = {
                    "order_id": tp_leg_id,
                    "ticker": ticker,
                    "side": "SELL",
                    "type": "TAKE_PROFIT",
                    "limit_price": order.take_profit,
                    "qty": qty,
                }
                legs.append({"id": tp_leg_id, "type": "take_profit", "limit_price": order.take_profit})

            new_pos = Position(
                position_id=pos_id,
                broker_position_id=f"broker_{pos_id}",
                ticker=ticker,
                side=OrderSide.BUY,
                qty=qty,
                entry_price=execution_price,
                current_price=execution_price,
                stop_loss=stop_price,
                take_profit=take_price,
                market_value=notional,
                unrealized_pnl=0.0,
                status=PositionStatus.OPEN,
                opened_at=datetime.now(timezone.utc),
            )
            self.positions[pos_id] = new_pos

            return OrderResult(
                order_id=order_id,
                client_order_id=order.client_order_id,
                ticker=ticker,
                status=OrderState.FILLED,
                filled_price=execution_price,
                filled_qty=qty,
                filled_dollar_amount=notional,
                fee=self.fee_per_trade,
                slippage=round(abs(execution_price - current_price) * qty, 4),
                raw_response={
                    "position_id": pos_id,
                    "legs": legs,
                    "stop_loss_order_id": sl_leg_id,
                    "take_profit_order_id": tp_leg_id,
                },
            )

        elif order.side == OrderSide.SELL:
            existing = self.get_position(ticker)
            if not existing:
                return OrderResult(
                    order_id=order_id,
                    client_order_id=order.client_order_id,
                    ticker=ticker,
                    status=OrderState.REJECTED_BY_BROKER,
                    error_message=f"No open position found for {ticker} to sell",
                )
            return self.close_position(existing.position_id)

        return OrderResult(
            order_id=order_id,
            client_order_id=order.client_order_id,
            ticker=ticker,
            status=OrderState.REJECTED_BY_BROKER,
            error_message="Unsupported order side",
        )

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self.pending_orders:
            del self.pending_orders[order_id]
            return True
        return False

    def close_position(
        self, position_id: str, dollar_amount: Optional[float] = None
    ) -> OrderResult:
        if position_id not in self.positions:
            return OrderResult(
                order_id=f"close_{uuid.uuid4().hex[:8]}",
                client_order_id="close_client",
                ticker="UNKNOWN",
                status=OrderState.REJECTED_BY_BROKER,
                error_message="Position not found",
            )

        pos = self.positions[position_id]
        if pos.status != PositionStatus.OPEN:
            return OrderResult(
                order_id=f"close_{uuid.uuid4().hex[:8]}",
                client_order_id="close_client",
                ticker=pos.ticker,
                status=OrderState.REJECTED_BY_BROKER,
                error_message="Position already closed",
            )

        current_price = self.price_feed.get(pos.ticker, pos.current_price)
        slippage_factor = 1.0 - (self.slippage_bps / 10000.0)
        exit_price = round(current_price * slippage_factor, 2)

        proceeds = round(pos.qty * exit_price, 2)
        realized = round((exit_price - pos.entry_price) * pos.qty, 2)

        self.cash += proceeds
        self.cash -= self.fee_per_trade
        self.realized_pnl += realized

        exit_reason = ExitReason.MANUAL_CLOSE
        if pos.stop_loss and exit_price <= pos.stop_loss:
            exit_reason = ExitReason.STOP_LOSS
        elif pos.take_profit and exit_price >= pos.take_profit:
            exit_reason = ExitReason.TAKE_PROFIT

        closed_pos = Position(
            position_id=pos.position_id,
            broker_position_id=pos.broker_position_id,
            ticker=pos.ticker,
            side=pos.side,
            qty=pos.qty,
            entry_price=pos.entry_price,
            current_price=exit_price,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            market_value=0.0,
            unrealized_pnl=0.0,
            status=PositionStatus.CLOSED,
            opened_at=pos.opened_at,
            closed_at=datetime.now(timezone.utc),
            realized_pnl=realized,
            exit_reason=exit_reason,
        )
        self.positions[position_id] = closed_pos

        # Remove open leg orders associated with this ticker
        leg_ids_to_remove = [
            oid for oid, ord_data in self.open_orders.items()
            if isinstance(ord_data, dict) and ord_data.get("ticker") == pos.ticker
        ]
        for oid in leg_ids_to_remove:
            self.open_orders.pop(oid, None)

        return OrderResult(
            order_id=f"close_{uuid.uuid4().hex[:8]}",
            client_order_id=f"close_{pos.position_id}",
            ticker=pos.ticker,
            status=OrderState.FILLED,
            filled_price=exit_price,
            filled_qty=pos.qty,
            filled_dollar_amount=proceeds,
            fee=self.fee_per_trade,
        )

    def modify_position(
        self,
        position_id: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> bool:
        if position_id not in self.positions:
            return False
        pos = self.positions[position_id]
        if pos.status != PositionStatus.OPEN:
            return False

        updated = Position(
            position_id=pos.position_id,
            broker_position_id=pos.broker_position_id,
            ticker=pos.ticker,
            side=pos.side,
            qty=pos.qty,
            entry_price=pos.entry_price,
            current_price=pos.current_price,
            stop_loss=stop_loss if stop_loss is not None else pos.stop_loss,
            take_profit=take_profit if take_profit is not None else pos.take_profit,
            market_value=pos.market_value,
            unrealized_pnl=pos.unrealized_pnl,
            status=PositionStatus.OPEN,
            opened_at=pos.opened_at,
        )
        self.positions[position_id] = updated
        return True

    def disconnect(self) -> None:
        self.connected = False
