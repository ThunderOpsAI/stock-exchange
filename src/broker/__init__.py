"""Broker adapters package."""
from src.broker.base import AbstractBrokerAdapter
from src.broker.simulated import SimulatedPaperBroker
from src.broker.alpaca import AlpacaPaperBroker
from src.broker.etoro import EtoroBrokerAdapter
from src.broker.factory import get_broker_adapter
from src.broker.gateway import ExecutionGateway, GatewayExecutionResult
from src.broker.reconciliation import (
    ReconciliationDiscrepancy,
    ReconciliationDiscrepancyType,
    ReconciliationEngine,
    ReconciliationResult,
    ReconciliationService,
)
from src.broker.protection import BrokerProtectionService

__all__ = [
    "AbstractBrokerAdapter",
    "SimulatedPaperBroker",
    "AlpacaPaperBroker",
    "EtoroBrokerAdapter",
    "get_broker_adapter",
    "ExecutionGateway",
    "GatewayExecutionResult",
    "ReconciliationDiscrepancyType",
    "ReconciliationDiscrepancy",
    "ReconciliationResult",
    "ReconciliationService",
    "ReconciliationEngine",
    "BrokerProtectionService",
]
