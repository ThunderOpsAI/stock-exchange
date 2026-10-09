"""
Abstract Broker Adapter Protocol.
Defines standard interface for Simulated, Alpaca, and eToro execution adapters.
"""

from abc import ABC, abstractmethod
from typing import Any, List, Optional

from src.domain.models import AccountBalance, OrderRequest, OrderResult, Position


class AbstractBrokerAdapter(ABC):
    """
    Abstract broker interface protocol.
    All broker adapters (Simulated, Alpaca, eToro) adhere strictly to this contract.
    """

    @property
    def supports_native_bracket(self) -> bool:
        """Returns True if the broker natively supports server-side bracket orders (SL/TP legs)."""
        return False

    def get_open_orders(self) -> List[Any]:
        """Retrieve open/pending orders from the broker."""
        return []

    @abstractmethod
    def connect(self) -> bool:
        """Establish connection or validate credentials with the broker."""
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """Verify API liveness and connectivity."""
        pass

    @abstractmethod
    def get_account_balance(self) -> AccountBalance:
        """Retrieve current cash, equity, buying power, and PnL."""
        pass

    @abstractmethod
    def get_positions(self) -> List[Position]:
        """Retrieve all currently active positions."""
        pass

    @abstractmethod
    def get_position(self, ticker: str) -> Optional[Position]:
        """Retrieve an active position for a specific ticker symbol."""
        pass

    @abstractmethod
    def submit_order(self, order: OrderRequest) -> OrderResult:
        """Submit a new order (Market, Limit, or Bracket with SL/TP)."""
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open/pending order by broker order ID."""
        pass

    @abstractmethod
    def close_position(
        self, position_id: str, dollar_amount: Optional[float] = None
    ) -> OrderResult:
        """Close/liquidate an open position fully or partially."""
        pass

    @abstractmethod
    def modify_position(
        self,
        position_id: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> bool:
        """Update stop-loss or take-profit price levels on an active position."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Gracefully terminate session and clean up resources."""
        pass
