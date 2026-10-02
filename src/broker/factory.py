"""
Broker Adapter Factory.
Instantiates broker adapters based on environment configuration or explicit type.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from src.broker.alpaca import AlpacaPaperBroker
from src.broker.base import AbstractBrokerAdapter
from src.broker.etoro import EtoroBrokerAdapter
from src.broker.simulated import SimulatedPaperBroker


def get_broker_adapter(
    broker_type: Optional[str] = None, config: Optional[Dict[str, Any]] = None
) -> AbstractBrokerAdapter:
    cfg = config or {}
    btype = (broker_type or os.getenv("BROKER_TYPE", "simulated")).lower()

    if btype == "simulated":
        return SimulatedPaperBroker(
            initial_cash=cfg.get("initial_cash", float(os.getenv("INITIAL_CASH", "100.0"))),
            slippage_bps=cfg.get("slippage_bps", 2.0),
            fee_per_trade=cfg.get("fee_per_trade", 0.0),
        )
    elif btype == "alpaca":
        return AlpacaPaperBroker(
            api_key=cfg.get("api_key", os.getenv("ALPACA_API_KEY")),
            secret_key=cfg.get("secret_key", os.getenv("ALPACA_SECRET_KEY")),
            base_url=cfg.get("base_url", os.getenv("ALPACA_BASE_URL")),
        )
    elif btype == "etoro":
        return EtoroBrokerAdapter(
            api_key=cfg.get("api_key", os.getenv("ETORO_API_KEY")),
            user_key=cfg.get("user_key", os.getenv("ETORO_USER_KEY")),
            base_url=cfg.get("base_url", os.getenv("ETORO_BASE_URL")),
        )
    else:
        raise ValueError(f"Unsupported broker type: {broker_type}. Choose 'simulated', 'alpaca', or 'etoro'.")
