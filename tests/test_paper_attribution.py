"""
Unit and integration tests for paper-trading calibration and attribution reports.
Covers Ticket 28 (P5-05):
- Reconciles predicted versus realized fills/return, slippage, risk veto effect, and strategy contribution.
"""

import json
import pytest

from src.backtest.attribution import (
    PaperAttributionEngine,
    PaperAttributionReport,
    RiskVetoAttributionRecord,
    StrategyAttribution,
    TradeCalibrationRecord,
)


def test_trade_calibration_reconciliation():
    engine = PaperAttributionEngine()
    trade_data = {
        "trade_id": "T101",
        "ticker": "AAPL",
        "strategy": "TREND_FOLLOWING",
        "qty": 10.0,
        "predicted_entry_price": 150.00,
        "realized_entry_price": 150.05,  # 5 cents adverse slippage = 3.33 bps
        "predicted_exit_price": 155.00,
        "realized_exit_price": 156.00,
    }

    cal = engine.calibrate_trade(trade_data)
    assert isinstance(cal, TradeCalibrationRecord)
    assert cal.entry_slippage_usd == 0.50  # 0.05 * 10
    assert pytest.approx(cal.entry_slippage_bps, 0.1) == 3.33
    assert cal.realized_pnl_usd == 59.50  # (156.00 - 150.05) * 10
    assert pytest.approx(cal.predicted_return_pct, 0.01) == 3.333
    assert pytest.approx(cal.realized_return_pct, 0.01) == 3.965
    assert pytest.approx(cal.return_delta_pct, 0.01) == 0.632


def test_risk_veto_counterfactual_evaluation():
    engine = PaperAttributionEngine()

    # Case 1: Vetoed candidate that tanked (loss avoided -> risk engine saved money)
    veto_bad = {
        "candidate_id": "C_BAD",
        "ticker": "DROP",
        "strategy": "BREAKOUT",
        "veto_reason": "CONCENTRATION_LIMIT_EXCEEDED",
        "screened_entry_price": 100.0,
        "hypothetical_exit_price": 90.0,
        "hypothetical_qty": 5.0,
    }
    rec_bad = engine.evaluate_veto(veto_bad)
    assert rec_bad.loss_avoided is True
    assert rec_bad.hypothetical_return_pct == -10.0
    assert rec_bad.hypothetical_pnl_usd == -50.0

    # Case 2: Vetoed candidate that rallied (missed gain)
    veto_good = {
        "candidate_id": "C_GOOD",
        "ticker": "MOON",
        "strategy": "TREND_FOLLOWING",
        "veto_reason": "MAX_SLOTS_REACHED",
        "screened_entry_price": 50.0,
        "hypothetical_exit_price": 60.0,
        "hypothetical_qty": 2.0,
    }
    rec_good = engine.evaluate_veto(veto_good)
    assert rec_good.loss_avoided is False
    assert rec_good.hypothetical_return_pct == 20.0
    assert rec_good.hypothetical_pnl_usd == 20.0


def test_comprehensive_attribution_report():
    engine = PaperAttributionEngine()

    trades = [
        {
            "trade_id": "T1",
            "ticker": "AAPL",
            "strategy": "TREND",
            "qty": 5.0,
            "predicted_entry_price": 100.0,
            "realized_entry_price": 100.02,
            "predicted_exit_price": 105.0,
            "realized_exit_price": 104.0,  # +3.98/share PnL
        },
        {
            "trade_id": "T2",
            "ticker": "MSFT",
            "strategy": "TREND",
            "qty": 2.0,
            "predicted_entry_price": 200.0,
            "realized_entry_price": 200.00,
            "predicted_exit_price": 210.0,
            "realized_exit_price": 212.0,  # +12.00/share PnL
        },
        {
            "trade_id": "T3",
            "ticker": "TSLA",
            "strategy": "MEAN_REV",
            "qty": 4.0,
            "predicted_entry_price": 150.0,
            "realized_entry_price": 150.10,
            "predicted_exit_price": 153.0,
            "realized_exit_price": 147.0,  # -3.10/share PnL
        },
    ]

    vetoes = [
        {
            "candidate_id": "V1",
            "ticker": "RISKY1",
            "strategy": "TREND",
            "veto_reason": "PAIRWISE_CORRELATION_CEILING",
            "screened_entry_price": 50.0,
            "hypothetical_exit_price": 40.0,  # -10 loss avoided
            "hypothetical_qty": 2.0,
        },
        {
            "candidate_id": "V2",
            "ticker": "RISKY2",
            "strategy": "MEAN_REV",
            "veto_reason": "EARNINGS_BLACKOUT",
            "screened_entry_price": 80.0,
            "hypothetical_exit_price": 84.0,  # +4 missed gain
            "hypothetical_qty": 1.0,
        },
    ]

    report = engine.generate_report(
        trades=trades,
        vetoed_candidates=vetoes,
        manifest_id="mfst_20260610_abc123",
        report_id="rep_test_001",
    )

    assert isinstance(report, PaperAttributionReport)
    assert report.total_trades == 3
    assert report.manifest_id == "mfst_20260610_abc123"
    assert report.code_version is not None

    # Check PnL tracking error
    # T1: pred = +25, real = +19.90
    # T2: pred = +20, real = +24.00
    # T3: pred = +12, real = -12.40
    # Pred total = 57.00, Real total = 31.50 -> tracking error = -25.50
    assert report.predicted_total_pnl_usd == 57.00
    assert report.realized_total_pnl_usd == 31.50
    assert report.pnl_tracking_error_usd == -25.50

    # Total slippage
    # T1: 0.02 * 5 = 0.10
    # T2: 0.00
    # T3: 0.10 * 4 = 0.40
    assert pytest.approx(report.total_slippage_usd, 0.01) == 0.50

    # Veto summary
    # V1 avoided $20 loss; V2 missed $4 gain. Net value = +$16.
    assert report.veto_summary["total_vetoes"] == 2
    assert report.veto_summary["losses_avoided_count"] == 1
    assert report.veto_summary["missed_gains_count"] == 1
    assert report.veto_summary["net_veto_value_usd"] == 16.0
    assert report.veto_summary["veto_precision_pct"] == 50.0

    # Strategy attribution
    assert "TREND" in report.strategy_attribution
    assert "MEAN_REV" in report.strategy_attribution
    trend_attr = report.strategy_attribution["TREND"]
    assert trend_attr.trade_count == 2
    assert trend_attr.winning_trades == 2
    assert trend_attr.win_rate == 100.0
    assert trend_attr.total_pnl_usd == 43.90

    mean_attr = report.strategy_attribution["MEAN_REV"]
    assert mean_attr.trade_count == 1
    assert mean_attr.winning_trades == 0
    assert mean_attr.win_rate == 0.0
    assert mean_attr.total_pnl_usd == -12.40

    # JSON serialization
    j_str = report.to_json()
    parsed = json.loads(j_str)
    assert parsed["report_id"] == "rep_test_001"
    assert len(parsed["trade_calibrations"]) == 3
    assert len(parsed["veto_records"]) == 2
