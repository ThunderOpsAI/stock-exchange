"""
Execution Cost Modeling, Slippage, Partial Fills, Gaps, and Corporate Actions.
Implements:
- CostModelConfig: Configurable parameters for bid/ask spread, market impact slippage,
  fixed/variable fees, partial fill rates, overnight gap executions, and corporate actions.
- ExecutionCostModel: Deterministic pricing engine that calculates effective execution prices,
  simulates partial fills, applies overnight gap slippage, and executes corporate actions (splits, dividends, delistings).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class CostModelConfig:
    half_spread_bps: float = 3.0
    slippage_bps: float = 2.0
    fixed_fee_per_trade_usd: float = 0.0
    variable_fee_pct: float = 0.0001  # e.g. 1 bps regulatory/clearing fee
    partial_fill_rate: float = 1.0  # 1.0 = 100% filled, 0.8 = 80% filled
    max_volume_participation_pct: float = 0.02  # max 2% of bar volume
    enable_gap_fill_model: bool = True
    corporate_actions: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionCostSummary:
    total_spread_cost_usd: float = 0.0
    total_slippage_cost_usd: float = 0.0
    total_fees_usd: float = 0.0
    total_costs_usd: float = 0.0
    gap_exits_count: int = 0
    partial_fills_count: int = 0
    corporate_actions_applied: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ExecutionCostModel:
    """
    Deterministic cost and execution realism engine for backtests.
    """

    def __init__(self, config: Optional[CostModelConfig] = None):
        self.config = config or CostModelConfig()
        self.summary = ExecutionCostSummary()

    def reset_summary(self) -> None:
        self.summary = ExecutionCostSummary()

    def calculate_entry_execution(
        self,
        raw_price: float,
        target_shares: float,
        bar_volume: Optional[float] = None,
    ) -> Tuple[float, float, float]:
        """
        Calculates realistic entry execution:
        - Price adjusted upwards by half-spread and entry slippage.
        - Shares adjusted if volume participation cap or partial fill rate is active.
        Returns: (effective_entry_price, filled_shares, transaction_fee_usd)
        """
        spread_pct = self.config.half_spread_bps / 10000.0
        slippage_pct = self.config.slippage_bps / 10000.0
        effective_price = raw_price * (1.0 + spread_pct + slippage_pct)

        # Quantity execution
        filled_qty = target_shares * self.config.partial_fill_rate
        if bar_volume is not None and bar_volume > 0:
            max_shares = bar_volume * self.config.max_volume_participation_pct
            if filled_qty > max_shares:
                filled_qty = max_shares

        if filled_qty < target_shares:
            self.summary.partial_fills_count += 1

        dollar_notional = effective_price * filled_qty
        fee = self.config.fixed_fee_per_trade_usd + (dollar_notional * self.config.variable_fee_pct)

        # Update cost accounting
        spread_cost = raw_price * spread_pct * filled_qty
        slippage_cost = raw_price * slippage_pct * filled_qty
        self.summary.total_spread_cost_usd += spread_cost
        self.summary.total_slippage_cost_usd += slippage_cost
        self.summary.total_fees_usd += fee
        self.summary.total_costs_usd += (spread_cost + slippage_cost + fee)

        return round(effective_price, 4), round(filled_qty, 4), round(fee, 4)

    def calculate_exit_execution(
        self,
        stop_price: float,
        bar_open: float,
        bar_low: float,
        shares: float,
        is_stop_loss: bool = True,
    ) -> Tuple[float, float, str]:
        """
        Calculates realistic exit execution:
        - If overnight gap occurs below stop loss (open < stop_price), fills at bar_open (slipping past stop).
        - Subtracts half-spread and exit slippage.
        Returns: (effective_exit_price, transaction_fee_usd, exit_reason)
        """
        exit_reason = "STOP_LOSS"
        base_price = stop_price

        # Check overnight gap-through
        if self.config.enable_gap_fill_model and is_stop_loss:
            if bar_open < stop_price:
                base_price = bar_open
                exit_reason = "GAP_STOP"
                self.summary.gap_exits_count += 1

        spread_pct = self.config.half_spread_bps / 10000.0
        slippage_pct = self.config.slippage_bps / 10000.0
        effective_price = base_price * (1.0 - spread_pct - slippage_pct)

        dollar_notional = effective_price * shares
        fee = self.config.fixed_fee_per_trade_usd + (dollar_notional * self.config.variable_fee_pct)

        spread_cost = base_price * spread_pct * shares
        slippage_cost = base_price * slippage_pct * shares
        self.summary.total_spread_cost_usd += spread_cost
        self.summary.total_slippage_cost_usd += slippage_cost
        self.summary.total_fees_usd += fee
        self.summary.total_costs_usd += (spread_cost + slippage_cost + fee)

        return round(effective_price, 4), round(fee, 4), exit_reason

    def process_corporate_actions_for_date(
        self,
        ticker: str,
        current_date_str: str,
        position_shares: float,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
    ) -> Tuple[float, float, float, float, float, Optional[str]]:
        """
        Applies corporate actions (splits, reverse splits, cash dividends, delistings) for a symbol on date.
        Returns: (new_shares, new_entry_price, new_stop_loss, new_take_profit, cash_dividend_usd, delisting_status)
        """
        new_shares = position_shares
        new_entry = entry_price
        new_sl = stop_loss
        new_tp = take_profit
        dividend_cash = 0.0
        delisting_status = None

        for action in self.config.corporate_actions:
            if action.get("ticker") != ticker:
                continue
            action_date = str(action.get("ex_date") or action.get("date"))[:10]
            if action_date != current_date_str[:10]:
                continue

            action_type = action.get("type", "").lower()
            if action_type in ("split", "stock_split"):
                ratio = float(action.get("ratio", 1.0))
                if ratio > 0:
                    new_shares = round(new_shares * ratio, 6)
                    new_entry = round(new_entry / ratio, 4)
                    new_sl = round(new_sl / ratio, 4)
                    new_tp = round(new_tp / ratio, 4)
                    self.summary.corporate_actions_applied += 1

            elif action_type in ("dividend", "cash_dividend"):
                amount_per_share = float(action.get("amount", 0.0))
                dividend_cash += round(new_shares * amount_per_share, 4)
                self.summary.corporate_actions_applied += 1

            elif action_type in ("delisting", "delist"):
                delisting_status = "DELISTED"
                self.summary.corporate_actions_applied += 1

        return new_shares, new_entry, new_sl, new_tp, dividend_cash, delisting_status
