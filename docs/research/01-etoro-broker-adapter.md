# Research Report: Ticket 01 — eToro Automation & Broker Adapter Interface Specification

## 1. Executive Summary & Critical Findings

1. **Official eToro Developer API Exists**: eToro provides an official **Public Developer API** (`https://public-api.etoro.com`) and Agent Portfolios framework accessible via `api-portal.etoro.com` and `builders.etoro.com`. It provides standard REST endpoints and WebSocket feeds for market data, portfolio balances, position inspection, and order execution (using `x-api-key`, `x-user-key`, and `x-request-id` headers), supporting both **Demo** and **Real** environments.
2. **Browser Automation (Playwright/Selenium) is High-Risk and Prohibited**: Automating eToro's web frontend via Playwright/Selenium faces severe anti-bot obstacles (Akamai Bot Manager, Cloudflare, CDP detection, dynamic TOTP/SMS challenges) and violates Section 15 of eToro's Terms of Service, carrying risks of immediate account bans. Browser automation is rejected.
3. **Decoupled Architecture**: The system establishes an `AbstractBrokerAdapter` protocol with three concrete implementations: `SimulatedPaperBroker` (local unit testing / backtesting), `AlpacaPaperBroker` (live market paper validation), and `EtoroBrokerAdapter` (official public API).

---

## 2. eToro Integration Mechanics

- **Authentication**: `x-api-key` (Application API Key), `x-user-key` (Secret User Key), `x-request-id` (Idempotent UUIDv4).
- **Core Endpoints**:
  - `GET /api/v1/market-data/search?query={symbol}` (maps ticker to numeric `instrumentId`).
  - `GET /api/v1/user/portfolio` & `GET /api/v1/trading/positions` (inspect equity and positions).
  - `POST /api/v2/orders` (execution parameters: `instrumentId`, `isBuy`, `amount` in USD, `leverage=1`, `stopLossRate`, `takeProfitRate`).
  - `POST /api/v1/trading/positions/{positionId}/close` (liquidate/close position).
- **Rate Limits**: 60 requests / 60 seconds (token-bucket limiter configured for 50 req/min with jittered backoff).
- **Capital & Fractionals**: eToro enforces a minimum order size of **$10**, perfectly matching our $20–$30 allocation slots.

---

## 3. Broker Adapter Interface Specification

### Domain Models (`schemas.py`)
```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class OrderSide(str, Enum):
  BUY = "BUY"
  SELL = "SELL"


class OrderType(str, Enum):
  MARKET = "MARKET"
  LIMIT = "LIMIT"


class OrderStatus(str, Enum):
  PENDING = "PENDING"
  FILLED = "FILLED"
  REJECTED = "REJECTED"
  CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class AccountBalance:
  cash: float
  equity: float
  buying_power: float
  currency: str = "USD"
  unrealized_pnl: float = 0.0
  realized_pnl: float = 0.0
  updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class Position:
  position_id: str
  ticker: str
  broker_symbol_id: str  # e.g., eToro instrumentId
  side: OrderSide
  qty: float
  entry_price: float
  current_price: float
  market_value: float
  unrealized_pnl: float
  unrealized_pnl_percent: float
  stop_loss_price: Optional[float] = None
  take_profit_price: Optional[float] = None
  opened_at: Optional[datetime] = None


@dataclass(frozen=True)
class OrderRequest:
  ticker: str
  side: OrderSide
  dollar_amount: float
  stop_loss: Optional[float] = None
  take_profit: Optional[float] = None
  order_type: OrderType = OrderType.MARKET
  limit_price: Optional[float] = None
  client_order_id: str = field(
      default_factory=lambda: f"ord_{datetime.utcnow().timestamp()}"
  )


@dataclass(frozen=True)
class OrderResult:
  order_id: str
  client_order_id: str
  ticker: str
  status: OrderStatus
  filled_price: Optional[float] = None
  filled_qty: Optional[float] = None
  filled_dollar_amount: Optional[float] = None
  fee: float = 0.0
  timestamp: datetime = field(default_factory=datetime.utcnow)
  error_message: Optional[str] = None
  raw_response: Optional[Dict[str, Any]] = None
```

### Abstract Base Class (`base.py`)
```python
from abc import ABC, abstractmethod
from typing import List, Optional


class AbstractBrokerAdapter(ABC):

  @abstractmethod
  def connect(self) -> bool:
    pass

  @abstractmethod
  def health_check(self) -> bool:
    pass

  @abstractmethod
  def get_account_balance(self) -> AccountBalance:
    pass

  @abstractmethod
  def get_positions(self) -> List[Position]:
    pass

  @abstractmethod
  def get_position(self, ticker: str) -> Optional[Position]:
    pass

  @abstractmethod
  def submit_order(self, order: OrderRequest) -> OrderResult:
    pass

  @abstractmethod
  def cancel_order(self, order_id: str) -> bool:
    pass

  @abstractmethod
  def close_position(
      self, position_id: str, dollar_amount: Optional[float] = None
  ) -> OrderResult:
    pass

  @abstractmethod
  def modify_position(
      self,
      position_id: str,
      stop_loss: Optional[float] = None,
      take_profit: Optional[float] = None,
  ) -> bool:
    pass

  @abstractmethod
  def disconnect(self) -> None:
    pass
```

---

## 4. Phased Adapter Rollout
1. **Tier 1 (SimulatedPaperBroker)**: In-memory/SQLite broker for offline testing and backtesting.
2. **Tier 2 (AlpacaPaperBroker)**: Live market data and execution forward testing via Alpaca Paper API.
3. **Tier 3 (EtoroBrokerAdapter)**: Direct official REST integration using `public-api.etoro.com` (Demo sandbox first, then Real).
