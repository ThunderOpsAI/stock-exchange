"""
Paper-Trading Calibration and Attribution Reports.
Covers Ticket 28 (P5-05):
- Reconciles predicted versus realized fills, returns, and slippage.
- Analyzes risk engine veto effects (counterfactual analysis of avoided losses vs missed gains).
- Provides strategy-level contribution and attribution metrics.
- Ties back to DataManifest and execution lineage.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.backtest.manifest import get_current_git_version


@dataclass
class TradeCalibrationRecord:
    trade_id: str
    ticker: str
    strategy: str
    qty: float
    predicted_entry_price: float
    realized_entry_price: float
    entry_slippage_usd: float
    entry_slippage_bps: float
    realized_pnl_usd: float
    predicted_exit_price: Optional[float] = None
    realized_exit_price: Optional[float] = None
    predicted_return_pct: Optional[float] = None
    realized_return_pct: Optional[float] = None
    return_delta_pct: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RiskVetoAttributionRecord:
    candidate_id: str
    ticker: str
    strategy: str
    veto_reason: str
    screened_entry_price: float
    hypothetical_exit_price: float
    hypothetical_return_pct: float
    hypothetical_pnl_usd: float
    loss_avoided: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StrategyAttribution:
    strategy: str
    trade_count: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl_usd: float
    avg_return_pct: float
    avg_slippage_bps: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PaperAttributionReport:
    report_id: str
    generated_at: str
    manifest_id: Optional[str]
    code_version: str
    total_trades: int
    predicted_total_pnl_usd: float
    realized_total_pnl_usd: float
    pnl_tracking_error_usd: float
    total_slippage_usd: float
    avg_slippage_bps: float
    trade_calibrations: List[TradeCalibrationRecord]
    veto_records: List[RiskVetoAttributionRecord]
    veto_summary: Dict[str, Any]
    strategy_attribution: Dict[str, StrategyAttribution]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "manifest_id": self.manifest_id,
            "code_version": self.code_version,
            "total_trades": self.total_trades,
            "predicted_total_pnl_usd": self.predicted_total_pnl_usd,
            "realized_total_pnl_usd": self.realized_total_pnl_usd,
            "pnl_tracking_error_usd": self.pnl_tracking_error_usd,
            "total_slippage_usd": self.total_slippage_usd,
            "avg_slippage_bps": self.avg_slippage_bps,
            "trade_calibrations": [t.to_dict() for t in self.trade_calibrations],
            "veto_records": [v.to_dict() for v in self.veto_records],
            "veto_summary": self.veto_summary,
            "strategy_attribution": {
                k: v.to_dict() for k, v in self.strategy_attribution.items()
            },
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


class PaperAttributionEngine:
    """
    Computes calibration and attribution reports reconciling paper execution
    against strategy predictions and counterfactual risk decisions.
    """

    def __init__(self, db: Optional[Any] = None):
        self.db = db

    def calibrate_trade(self, trade_data: Dict[str, Any]) -> TradeCalibrationRecord:
        """
        Calibrates an individual trade reconciling predicted entry/exit/return
        with realized fills and slippage.
        """
        trade_id = str(trade_data.get("trade_id", trade_data.get("id", "trade_unk")))
        ticker = trade_data.get("ticker", "UNKNOWN")
        strategy = trade_data.get("strategy", "DEFAULT")
        qty = float(trade_data.get("qty", trade_data.get("shares", 1.0)))

        pred_entry = float(trade_data.get("predicted_entry_price", trade_data.get("entry_price", 0.0)))
        real_entry = float(trade_data.get("realized_entry_price", trade_data.get("fill_price", pred_entry)))

        # Slippage: for a buy, positive slippage means fill > predicted (adverse)
        slippage_per_share = real_entry - pred_entry
        total_slippage_usd = round(slippage_per_share * qty, 4)
        slippage_bps = round((slippage_per_share / pred_entry * 10_000.0) if pred_entry > 0 else 0.0, 2)

        pred_exit = float(trade_data["predicted_exit_price"]) if "predicted_exit_price" in trade_data and trade_data["predicted_exit_price"] is not None else None
        real_exit = float(trade_data["realized_exit_price"]) if "realized_exit_price" in trade_data and trade_data["realized_exit_price"] is not None else None

        realized_pnl = float(trade_data.get("realized_pnl_usd", 0.0))
        if realized_pnl == 0.0 and real_exit is not None:
            realized_pnl = round((real_exit - real_entry) * qty, 2)

        pred_ret = None
        if pred_exit is not None and pred_entry > 0:
            pred_ret = round((pred_exit - pred_entry) / pred_entry * 100.0, 3)

        real_ret = None
        if real_exit is not None and real_entry > 0:
            real_ret = round((real_exit - real_entry) / real_entry * 100.0, 3)

        ret_delta = None
        if pred_ret is not None and real_ret is not None:
            ret_delta = round(real_ret - pred_ret, 3)

        return TradeCalibrationRecord(
            trade_id=trade_id,
            ticker=ticker,
            strategy=strategy,
            qty=qty,
            predicted_entry_price=pred_entry,
            realized_entry_price=real_entry,
            entry_slippage_usd=total_slippage_usd,
            entry_slippage_bps=slippage_bps,
            realized_pnl_usd=realized_pnl,
            predicted_exit_price=pred_exit,
            realized_exit_price=real_exit,
            predicted_return_pct=pred_ret,
            realized_return_pct=real_ret,
            return_delta_pct=ret_delta,
        )

    def evaluate_veto(self, veto_data: Dict[str, Any]) -> RiskVetoAttributionRecord:
        """
        Evaluates the counterfactual performance of a candidate blocked by the risk engine.
        """
        candidate_id = str(veto_data.get("candidate_id", "cand_unk"))
        ticker = veto_data.get("ticker", "UNKNOWN")
        strategy = veto_data.get("strategy", "DEFAULT")
        veto_reason = veto_data.get("veto_reason", "RISK_VETO")
        entry_price = float(veto_data.get("screened_entry_price", veto_data.get("price", 100.0)))
        exit_price = float(veto_data.get("hypothetical_exit_price", entry_price))
        qty = float(veto_data.get("hypothetical_qty", 1.0))

        hyp_return_pct = round(((exit_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0, 3)
        hyp_pnl_usd = round((exit_price - entry_price) * qty, 2)
        loss_avoided = hyp_pnl_usd < 0.0

        return RiskVetoAttributionRecord(
            candidate_id=candidate_id,
            ticker=ticker,
            strategy=strategy,
            veto_reason=veto_reason,
            screened_entry_price=entry_price,
            hypothetical_exit_price=exit_price,
            hypothetical_return_pct=hyp_return_pct,
            hypothetical_pnl_usd=hyp_pnl_usd,
            loss_avoided=loss_avoided,
        )

    def generate_report(
        self,
        trades: List[Dict[str, Any]],
        vetoed_candidates: Optional[List[Dict[str, Any]]] = None,
        manifest_id: Optional[str] = None,
        report_id: Optional[str] = None,
    ) -> PaperAttributionReport:
        """
        Generates a comprehensive attribution report reconciling trades, slippage,
        risk veto effects, and strategy contributions.
        """
        now = datetime.now(timezone.utc)
        rep_id = report_id or f"rep_{now.strftime('%Y%m%d_%H%M%S')}"
        code_ver = get_current_git_version()

        calibrated_trades = [self.calibrate_trade(t) for t in trades]
        veto_records = [self.evaluate_veto(v) for v in (vetoed_candidates or [])]

        # Aggregate trade statistics
        total_trades = len(calibrated_trades)
        realized_total_pnl = round(sum(t.realized_pnl_usd for t in calibrated_trades), 2)

        predicted_pnl_sum = 0.0
        for t in calibrated_trades:
            if t.predicted_exit_price is not None and t.predicted_entry_price > 0:
                predicted_pnl_sum += (t.predicted_exit_price - t.predicted_entry_price) * t.qty
            else:
                predicted_pnl_sum += t.realized_pnl_usd
        predicted_total_pnl = round(predicted_pnl_sum, 2)
        pnl_tracking_error = round(realized_total_pnl - predicted_total_pnl, 2)

        total_slippage = round(sum(t.entry_slippage_usd for t in calibrated_trades), 4)
        avg_slippage_bps = round(
            sum(t.entry_slippage_bps for t in calibrated_trades) / total_trades if total_trades > 0 else 0.0,
            2,
        )

        # Aggregate veto statistics
        losses_avoided = sum(1 for v in veto_records if v.loss_avoided)
        missed_gains = sum(1 for v in veto_records if not v.loss_avoided and v.hypothetical_pnl_usd > 0)
        neutral = len(veto_records) - losses_avoided - missed_gains
        # Net value added by risk vetoes = avoided losses (+ve) minus missed gains
        net_veto_value = round(sum(-v.hypothetical_pnl_usd for v in veto_records), 2)

        veto_summary = {
            "total_vetoes": len(veto_records),
            "losses_avoided_count": losses_avoided,
            "missed_gains_count": missed_gains,
            "neutral_count": neutral,
            "net_veto_value_usd": net_veto_value,
            "veto_precision_pct": round(losses_avoided / len(veto_records) * 100.0, 1) if veto_records else 0.0,
        }

        # Strategy attribution breakdown
        strat_groups: Dict[str, List[TradeCalibrationRecord]] = {}
        for t in calibrated_trades:
            strat_groups.setdefault(t.strategy, []).append(t)

        strategy_attribution: Dict[str, StrategyAttribution] = {}
        for strat, str_trades in strat_groups.items():
            n = len(str_trades)
            wins = sum(1 for t in str_trades if t.realized_pnl_usd > 0)
            losses = sum(1 for t in str_trades if t.realized_pnl_usd < 0)
            tot_pnl = round(sum(t.realized_pnl_usd for t in str_trades), 2)
            win_rate = round(wins / n * 100.0 if n > 0 else 0.0, 1)
            returns = [t.realized_return_pct for t in str_trades if t.realized_return_pct is not None]
            avg_ret = round(sum(returns) / len(returns), 3) if returns else 0.0
            avg_slip = round(sum(t.entry_slippage_bps for t in str_trades) / n, 2) if n > 0 else 0.0

            strategy_attribution[strat] = StrategyAttribution(
                strategy=strat,
                trade_count=n,
                winning_trades=wins,
                losing_trades=losses,
                win_rate=win_rate,
                total_pnl_usd=tot_pnl,
                avg_return_pct=avg_ret,
                avg_slippage_bps=avg_slip,
            )

        return PaperAttributionReport(
            report_id=rep_id,
            generated_at=now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            manifest_id=manifest_id,
            code_version=code_ver,
            total_trades=total_trades,
            predicted_total_pnl_usd=predicted_total_pnl,
            realized_total_pnl_usd=realized_total_pnl,
            pnl_tracking_error_usd=pnl_tracking_error,
            total_slippage_usd=total_slippage,
            avg_slippage_bps=avg_slippage_bps,
            trade_calibrations=calibrated_trades,
            veto_records=veto_records,
            veto_summary=veto_summary,
            strategy_attribution=strategy_attribution,
        )
