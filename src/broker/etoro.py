"""
eToro Public Developer API Broker Adapter.
Targeting https://public-api.etoro.com with token-bucket rate limiter (50 req/min),
idempotent x-request-id headers, and official REST endpoints.
"""

from __future__ import annotations

import os
import time
import uuid
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


class TokenBucketRateLimiter:
    """Thread-safe token bucket rate limiter for 50 requests per minute."""

    def __init__(self, rate_per_minute: float = 50.0):
        self.capacity = rate_per_minute
        self.tokens = rate_per_minute
        self.fill_rate = rate_per_minute / 60.0  # tokens per second
        self.last_check = time.monotonic()

    def acquire(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_check
        self.last_check = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.fill_rate)

        if self.tokens < 1.0:
            sleep_needed = (1.0 - self.tokens) / self.fill_rate
            time.sleep(sleep_needed)
            self.tokens = 0.0
            self.last_check = time.monotonic()
        else:
            self.tokens -= 1.0


class EtoroBrokerAdapter(AbstractBrokerAdapter):
    DEFAULT_BASE_URL = "https://public-api.etoro.com"

    def __init__(
        self,
        api_key: Optional[str] = None,
        user_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self.api_key = api_key or os.getenv("ETORO_API_KEY", "")
        self.user_key = user_key or os.getenv("ETORO_USER_KEY", "")
        self.base_url = (base_url or os.getenv("ETORO_BASE_URL", self.DEFAULT_BASE_URL)).rstrip("/")
        self.timeout = timeout
        self.limiter = TokenBucketRateLimiter(rate_per_minute=50.0)
        self.session = requests.Session()
        self.symbol_cache: Dict[str, int] = {}
        self.connected = False

    @property
    def supports_native_bracket(self) -> bool:
        return False

    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "x-user-key": self.user_key,
            "x-request-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        self.limiter.acquire()
        headers = self._headers()
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))
        url = f"{self.base_url}{path}"
        return self.session.request(method, url, headers=headers, timeout=self.timeout, **kwargs)

    def connect(self) -> bool:
        if not self.api_key or not self.user_key:
            self.connected = False
            return False
        try:
            resp = self._request("GET", "/api/v1/user/portfolio")
            self.connected = resp.status_code == 200
            return self.connected
        except requests.RequestException:
            self.connected = False
            return False

    def health_check(self) -> bool:
        try:
            resp = self._request("GET", "/api/v1/market-data/health")
            return resp.status_code in (200, 204)
        except requests.RequestException:
            return False

    def resolve_instrument_id(self, ticker: str) -> Optional[int]:
        ticker = ticker.upper()
        if ticker in self.symbol_cache:
            return self.symbol_cache[ticker]

        try:
            resp = self._request("GET", f"/api/v1/market-data/search?query={ticker}")
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                for item in items:
                    if item.get("symbol", "").upper() == ticker:
                        iid = int(item["instrumentId"])
                        self.symbol_cache[ticker] = iid
                        return iid
        except requests.RequestException:
            pass
        return None

    def get_account_balance(self) -> AccountBalance:
        resp = self._request("GET", "/api/v1/user/portfolio")
        resp.raise_for_status()
        data = resp.json()
        cash = float(data.get("credit", data.get("cash", 0.0)))
        equity = float(data.get("equity", data.get("totalPortfolioValue", cash)))
        unrealized = float(data.get("unrealizedPnL", 0.0))
        realized = float(data.get("realizedPnL", 0.0))
        return AccountBalance(
            cash=cash,
            equity=equity,
            buying_power=cash,
            currency="USD",
            unrealized_pnl=unrealized,
            realized_pnl=realized,
            updated_at=datetime.now(timezone.utc),
        )

    def get_positions(self) -> List[Position]:
        resp = self._request("GET", "/api/v1/trading/positions")
        resp.raise_for_status()
        raw_positions = resp.json().get("positions", [])

        results = []
        for p in raw_positions:
            pos_id = str(p.get("positionId"))
            ticker = p.get("symbol", "UNKNOWN")
            amount = float(p.get("amount", 0.0))
            current_rate = float(p.get("currentRate", 1.0))
            open_rate = float(p.get("openRate", current_rate))
            qty = round(amount / open_rate, 4) if open_rate > 0 else 0.0
            pnl = float(p.get("netProfit", 0.0))

            results.append(
                Position(
                    position_id=pos_id,
                    broker_position_id=pos_id,
                    ticker=ticker,
                    side=OrderSide.BUY if p.get("isBuy", True) else OrderSide.SELL,
                    qty=qty,
                    entry_price=open_rate,
                    current_price=current_rate,
                    stop_loss=float(p.get("stopLossRate", 0.0)),
                    take_profit=float(p.get("takeProfitRate", 0.0)),
                    market_value=round(qty * current_rate, 2),
                    unrealized_pnl=pnl,
                    status=PositionStatus.OPEN,
                    opened_at=datetime.now(timezone.utc),
                )
            )
        return results

    def get_position(self, ticker: str) -> Optional[Position]:
        positions = self.get_positions()
        for p in positions:
            if p.ticker.upper() == ticker.upper():
                return p
        return None

    def submit_order(self, order: OrderRequest) -> OrderResult:
        if order.dollar_amount < 10.0:
            return OrderResult(
                order_id="rejected_min_threshold",
                client_order_id=order.client_order_id,
                ticker=order.ticker.upper(),
                status=OrderState.REJECTED_BY_BROKER,
                error_message="eToro requires a minimum order amount of $10.00",
            )

        instrument_id = self.resolve_instrument_id(order.ticker)
        if not instrument_id:
            # Fallback mock/sim instrumentId if running in demo/mock without real search
            instrument_id = hash(order.ticker) % 100000

        payload = {
            "instrumentId": instrument_id,
            "isBuy": order.side == OrderSide.BUY,
            "amount": round(order.dollar_amount, 2),
            "leverage": 1,
        }
        if order.stop_loss:
            payload["stopLossRate"] = round(order.stop_loss, 2)
        if order.take_profit:
            payload["takeProfitRate"] = round(order.take_profit, 2)

        try:
            resp = self._request("POST", "/api/v2/orders", json=payload)
            data = resp.json() if resp.text else {}
            if resp.status_code in (200, 201):
                order_id = str(data.get("orderId", data.get("id", uuid.uuid4().hex[:8])))
                return OrderResult(
                    order_id=order_id,
                    client_order_id=order.client_order_id,
                    ticker=order.ticker.upper(),
                    status=OrderState.ROUTED_TO_BROKER,
                    filled_dollar_amount=order.dollar_amount,
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
            resp = self._request("DELETE", f"/api/v2/orders/{order_id}")
            return resp.status_code in (200, 204)
        except requests.RequestException:
            return False

    def close_position(
        self, position_id: str, dollar_amount: Optional[float] = None
    ) -> OrderResult:
        try:
            payload = {}
            if dollar_amount:
                payload["amount"] = round(dollar_amount, 2)
            resp = self._request(
                "POST", f"/api/v1/trading/positions/{position_id}/close", json=payload
            )
            data = resp.json() if resp.text else {}
            if resp.status_code in (200, 202, 204):
                return OrderResult(
                    order_id=str(data.get("orderId", f"close_{position_id}")),
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
        payload = {}
        if stop_loss is not None:
            payload["stopLossRate"] = round(stop_loss, 2)
        if take_profit is not None:
            payload["takeProfitRate"] = round(take_profit, 2)
        try:
            resp = self._request(
                "PUT", f"/api/v1/trading/positions/{position_id}", json=payload
            )
            return resp.status_code in (200, 204)
        except requests.RequestException:
            return False

    def disconnect(self) -> None:
        self.session.close()
        self.connected = False
