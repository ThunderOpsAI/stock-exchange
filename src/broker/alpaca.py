"""
Alpaca Paper Trading Broker Adapter.
Connects to official Alpaca Paper Trading REST API (v2) for live forward validation.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import requests

from src.broker.base import AbstractBrokerAdapter
from src.domain.models import (
    AccountBalance,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderState,
    Position,
    PositionStatus,
)


class AlpacaPaperBroker(AbstractBrokerAdapter):
    DEFAULT_BASE_URL = "https://paper-api.alpaca.markets/v2"

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self.api_key = api_key or os.getenv("ALPACA_API_KEY", "")
        self.secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY", "")
        self.base_url = (base_url or os.getenv("ALPACA_BASE_URL", self.DEFAULT_BASE_URL)).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
                "Content-Type": "application/json",
            }
        )
        self.connected = False

    @property
    def supports_native_bracket(self) -> bool:
        return True

    def get_open_orders(self) -> List[Any]:
        try:
            resp = self.session.get(f"{self.base_url}/orders?status=open", timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json()
            return []
        except requests.RequestException:
            return []

    def connect(self) -> bool:
        if not self.api_key or not self.secret_key:
            self.connected = False
            return False
        try:
            resp = self.session.get(f"{self.base_url}/account", timeout=self.timeout)
            self.connected = resp.status_code == 200
            return self.connected
        except requests.RequestException:
            self.connected = False
            return False

    def health_check(self) -> bool:
        try:
            resp = self.session.get(f"{self.base_url}/clock", timeout=self.timeout)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def get_account_balance(self) -> AccountBalance:
        resp = self.session.get(f"{self.base_url}/account", timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        cash = float(data.get("cash", 0.0))
        equity = float(data.get("equity", data.get("portfolio_value", cash)))
        buying_power = float(data.get("buying_power", cash))
        return AccountBalance(
            cash=cash,
            equity=equity,
            buying_power=buying_power,
            currency=data.get("currency", "USD"),
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            updated_at=datetime.now(timezone.utc),
        )

    def get_positions(self) -> List[Position]:
        resp = self.session.get(f"{self.base_url}/positions", timeout=self.timeout)
        resp.raise_for_status()
        positions_raw = resp.json()

        results = []
        for p in positions_raw:
            qty = float(p.get("qty", 0.0))
            entry_price = float(p.get("avg_entry_price", 0.0))
            current_price = float(p.get("current_price", entry_price))
            market_value = float(p.get("market_value", qty * current_price))
            unrealized_pnl = float(p.get("unrealized_pl", 0.0))
            results.append(
                Position(
                    position_id=p.get("asset_id", p.get("symbol")),
                    broker_position_id=p.get("asset_id"),
                    ticker=p.get("symbol"),
                    side=OrderSide.BUY if p.get("side") == "long" else OrderSide.SELL,
                    qty=qty,
                    entry_price=entry_price,
                    current_price=current_price,
                    stop_loss=0.0,
                    take_profit=0.0,
                    market_value=market_value,
                    unrealized_pnl=unrealized_pnl,
                    status=PositionStatus.OPEN,
                    opened_at=datetime.now(timezone.utc),
                )
            )
        return results

    def get_position(self, ticker: str) -> Optional[Position]:
        try:
            resp = self.session.get(
                f"{self.base_url}/positions/{ticker.upper()}", timeout=self.timeout
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            p = resp.json()
            qty = float(p.get("qty", 0.0))
            entry_price = float(p.get("avg_entry_price", 0.0))
            current_price = float(p.get("current_price", entry_price))
            market_value = float(p.get("market_value", qty * current_price))
            unrealized_pnl = float(p.get("unrealized_pl", 0.0))
            return Position(
                position_id=p.get("asset_id", p.get("symbol")),
                broker_position_id=p.get("asset_id"),
                ticker=p.get("symbol"),
                side=OrderSide.BUY if p.get("side") == "long" else OrderSide.SELL,
                qty=qty,
                entry_price=entry_price,
                current_price=current_price,
                stop_loss=0.0,
                take_profit=0.0,
                market_value=market_value,
                unrealized_pnl=unrealized_pnl,
                status=PositionStatus.OPEN,
                opened_at=datetime.now(timezone.utc),
            )
        except requests.RequestException:
            return None

    def submit_order(self, order: OrderRequest) -> OrderResult:
        payload: Dict[str, Any] = {
            "symbol": order.ticker.upper(),
            "side": order.side.value.lower(),
            "type": order.order_type.value.lower(),
            "time_in_force": "day",
            "client_order_id": order.client_order_id,
        }

        # Dollar amount notional vs qty
        if order.target_qty and order.target_qty > 0:
            payload["qty"] = str(round(order.target_qty, 4))
        else:
            payload["notional"] = str(round(order.dollar_amount, 2))

        # Bracket orders
        if order.stop_loss and order.take_profit:
            payload["order_class"] = "bracket"
            payload["take_profit"] = {"limit_price": str(round(order.take_profit, 2))}
            payload["stop_loss"] = {"stop_price": str(round(order.stop_loss, 2))}

        try:
            resp = self.session.post(
                f"{self.base_url}/orders", json=payload, timeout=self.timeout
            )
            data = resp.json()
            if resp.status_code in (200, 201):
                return OrderResult(
                    order_id=data.get("id"),
                    client_order_id=data.get("client_order_id", order.client_order_id),
                    ticker=order.ticker.upper(),
                    status=OrderState.ROUTED_TO_BROKER,
                    raw_response=data,
                )
            else:
                return OrderResult(
                    order_id="failed",
                    client_order_id=order.client_order_id,
                    ticker=order.ticker.upper(),
                    status=OrderState.REJECTED_BY_BROKER,
                    error_message=str(data.get("message", resp.text)),
                    raw_response=data,
                )
        except requests.RequestException as e:
            return OrderResult(
                order_id="error",
                client_order_id=order.client_order_id,
                ticker=order.ticker.upper(),
                status=OrderState.REJECTED_BY_BROKER,
                error_message=str(e),
            )

    def cancel_order(self, order_id: str) -> bool:
        try:
            resp = self.session.delete(f"{self.base_url}/orders/{order_id}", timeout=self.timeout)
            return resp.status_code in (200, 204)
        except requests.RequestException:
            return False

    def close_position(
        self, position_id: str, dollar_amount: Optional[float] = None
    ) -> OrderResult:
        try:
            resp = self.session.delete(
                f"{self.base_url}/positions/{position_id}", timeout=self.timeout
            )
            data = resp.json() if resp.text else {}
            if resp.status_code in (200, 204):
                return OrderResult(
                    order_id=data.get("id", f"close_{position_id}"),
                    client_order_id=f"close_{position_id}",
                    ticker=position_id,
                    status=OrderState.FILLED,
                    raw_response=data,
                )
            else:
                return OrderResult(
                    order_id="failed",
                    client_order_id=f"close_{position_id}",
                    ticker=position_id,
                    status=OrderState.REJECTED_BY_BROKER,
                    error_message=str(data.get("message", resp.text)),
                )
        except requests.RequestException as e:
            return OrderResult(
                order_id="error",
                client_order_id=f"close_{position_id}",
                ticker=position_id,
                status=OrderState.REJECTED_BY_BROKER,
                error_message=str(e),
            )

    def modify_position(
        self,
        position_id: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> bool:
        # In Alpaca, brackets on positions are modified by replacing open stop/limit legs
        return True

    def disconnect(self) -> None:
        self.session.close()
        self.connected = False
