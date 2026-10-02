"""
Unit tests for Broker Adapters: Simulated, Alpaca, and eToro.
Tests orders, fills, fractional sizing, rate-limiting, and error handling.
"""

from unittest.mock import MagicMock, patch
import pytest

from src.broker.alpaca import AlpacaPaperBroker
from src.broker.base import AbstractBrokerAdapter
from src.broker.etoro import EtoroBrokerAdapter, TokenBucketRateLimiter
from src.broker.factory import get_broker_adapter
from src.broker.simulated import SimulatedPaperBroker
from src.domain.models import OrderRequest, OrderSide, OrderState, OrderType, PositionStatus


def test_simulated_broker_lifecycle():
    broker = SimulatedPaperBroker(initial_cash=100.0, slippage_bps=0.0)
    assert broker.connect() is True
    assert broker.health_check() is True

    # Initial balance check
    bal = broker.get_account_balance()
    assert bal.cash == 100.0
    assert bal.equity == 100.0

    # Set mock price for AAPL
    broker.set_price("AAPL", 150.0)

    # 1. Buy order with $28.50
    order = OrderRequest(
        ticker="AAPL",
        side=OrderSide.BUY,
        dollar_amount=28.50,
        stop_loss=145.0,
        take_profit=160.0,
    )
    result = broker.submit_order(order)
    assert result.status == OrderState.FILLED
    assert result.filled_price == 150.0
    # 28.50 / 150 = 0.1900
    assert result.filled_qty == 0.19
    assert result.filled_dollar_amount == 28.50

    # Verify balance & positions
    bal2 = broker.get_account_balance()
    assert bal2.cash == 71.50
    assert bal2.equity == 100.0
    positions = broker.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.ticker == "AAPL"
    assert pos.qty == 0.19
    assert pos.stop_loss == 145.0
    assert pos.take_profit == 160.0

    # 2. Modify position SL / TP
    modified = broker.modify_position(pos.position_id, stop_loss=147.0, take_profit=165.0)
    assert modified is True
    updated_pos = broker.get_position("AAPL")
    assert updated_pos.stop_loss == 147.0
    assert updated_pos.take_profit == 165.0

    # 3. Simulate price gain to $160.0
    broker.set_price("AAPL", 160.0)
    bal3 = broker.get_account_balance()
    # 0.19 * 160 = 30.40 -> unrealized +1.90 -> equity 71.50 + 30.40 = 101.90
    assert bal3.equity == 101.90
    assert bal3.unrealized_pnl == 1.90

    # 4. Close position
    close_result = broker.close_position(pos.position_id)
    assert close_result.status == OrderState.FILLED
    assert len(broker.get_positions()) == 0

    bal_final = broker.get_account_balance()
    assert bal_final.cash == 101.90
    assert bal_final.equity == 101.90
    assert bal_final.realized_pnl == 1.90


def test_simulated_broker_insufficient_funds():
    broker = SimulatedPaperBroker(initial_cash=20.0)
    broker.set_price("MSFT", 200.0)
    order = OrderRequest(
        ticker="MSFT",
        side=OrderSide.BUY,
        dollar_amount=28.50,
    )
    result = broker.submit_order(order)
    assert result.status == OrderState.REJECTED_BY_BROKER
    assert "Insufficient cash" in result.error_message


def test_etoro_rate_limiter():
    limiter = TokenBucketRateLimiter(rate_per_minute=1200.0)  # fast for test
    limiter.acquire()
    assert limiter.tokens <= 1200.0


def test_etoro_broker_adapter_mocked():
    adapter = EtoroBrokerAdapter(
        api_key="mock_api_key",
        user_key="mock_user_key",
        base_url="https://mock-etoro.com",
    )

    # 1. Under $10.00 minimum threshold rejection
    small_order = OrderRequest(
        ticker="SPY",
        side=OrderSide.BUY,
        dollar_amount=8.50,
    )
    res_small = adapter.submit_order(small_order)
    assert res_small.status == OrderState.REJECTED_BY_BROKER
    assert "minimum order amount of $10.00" in res_small.error_message

    # 2. Mock order submission
    with patch.object(adapter.session, "request") as mock_req:
        # Mock search response
        search_resp = MagicMock()
        search_resp.status_code = 200
        search_resp.json.return_value = {
            "items": [{"symbol": "SPY", "instrumentId": 1055}]
        }

        # Mock order response
        order_resp = MagicMock()
        order_resp.status_code = 200
        order_resp.text = '{"orderId": "etoro_ord_999"}'
        order_resp.json.return_value = {"orderId": "etoro_ord_999"}

        mock_req.side_effect = [search_resp, order_resp]

        order = OrderRequest(
            ticker="SPY",
            side=OrderSide.BUY,
            dollar_amount=29.00,
            stop_loss=490.0,
            take_profit=520.0,
        )
        res = adapter.submit_order(order)
        assert res.status == OrderState.ROUTED_TO_BROKER
        assert res.order_id == "etoro_ord_999"


def test_alpaca_broker_adapter_mocked():
    adapter = AlpacaPaperBroker(
        api_key="mock_alpaca_key",
        secret_key="mock_alpaca_secret",
        base_url="https://paper-api.alpaca.markets/v2",
    )

    with patch.object(adapter.session, "get") as mock_get, patch.object(adapter.session, "post") as mock_post:
        # Account balance mock
        acc_resp = MagicMock()
        acc_resp.status_code = 200
        acc_resp.json.return_value = {
            "cash": "98.50",
            "portfolio_value": "98.50",
            "buying_power": "98.50",
            "currency": "USD",
        }
        mock_get.return_value = acc_resp

        bal = adapter.get_account_balance()
        assert bal.cash == 98.50
        assert bal.equity == 98.50

        # Bracket order mock
        ord_resp = MagicMock()
        ord_resp.status_code = 201
        ord_resp.json.return_value = {
            "id": "alpaca_bracket_001",
            "client_order_id": "client_ord_1",
            "symbol": "QQQ",
        }
        mock_post.return_value = ord_resp

        order = OrderRequest(
            ticker="QQQ",
            side=OrderSide.BUY,
            dollar_amount=28.00,
            stop_loss=430.0,
            take_profit=450.0,
            client_order_id="client_ord_1",
        )
        res = adapter.submit_order(order)
        assert res.status == OrderState.ROUTED_TO_BROKER
        assert res.order_id == "alpaca_bracket_001"


def test_broker_factory():
    sim = get_broker_adapter("simulated", {"initial_cash": 100.0})
    assert isinstance(sim, SimulatedPaperBroker)
    assert sim.initial_cash == 100.0

    alpaca = get_broker_adapter("alpaca", {"api_key": "k", "secret_key": "s"})
    assert isinstance(alpaca, AlpacaPaperBroker)

    etoro = get_broker_adapter("etoro", {"api_key": "k", "user_key": "u"})
    assert isinstance(etoro, EtoroBrokerAdapter)

    with pytest.raises(ValueError):
        get_broker_adapter("unknown_broker")
