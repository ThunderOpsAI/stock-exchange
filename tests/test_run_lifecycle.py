"""Singleton run lease, lifecycle and restart recovery (P2-06)."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from src.main import TradingDeskOrchestrator
from src.storage.db import Database


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    d = Database(db_path=path)
    yield d
    os.remove(path)


def test_concurrent_run_rejected(db):
    assert db.acquire_run_lease("r1") is True
    assert db.acquire_run_lease("r2") is False
    assert db.get_trading_run("r2") is None


def test_release_allows_new_run(db):
    assert db.acquire_run_lease("r1")
    db.release_run_lease("r1", "COMPLETED")
    assert db.get_trading_run("r1")["status"] == "COMPLETED"
    assert db.acquire_run_lease("r2")


def test_stale_lease_recovered_and_marked_interrupted(db):
    assert db.acquire_run_lease("r1", lease_timeout_seconds=-1)
    assert db.acquire_run_lease("r2")
    old = db.get_trading_run("r1")
    assert old["status"] == "INTERRUPTED"
    assert old["error_message"] == "STALE_LEASE_RECOVERED"
    assert db.get_active_trading_run()["run_id"] == "r2"


def test_renew_extends_lease(db):
    db.acquire_run_lease("r1", lease_timeout_seconds=1)
    assert db.renew_run_lease("r1", lease_timeout_seconds=600)
    assert db.acquire_run_lease("r2") is False


def test_orchestrator_rejects_when_lease_held(db):
    db.acquire_run_lease("other")
    orch = TradingDeskOrchestrator(db_path=db.db_path, universe=["SPY"])
    assert orch.run_daily_cycle()["status"] == "REJECTED_LEASE_HELD"


def test_orchestrator_blocks_on_unclean_reconciliation_and_records_outcome(db):
    orch = TradingDeskOrchestrator(db_path=db.db_path, universe=["SPY"])
    # Local DB holds an UNKNOWN intent the fresh simulated broker has never seen.
    db.save_order_intent(
        intent_id="i1", request_hash="h1", ticker="AAPL", side="BUY", target_qty=0.1,
        allocated_usd=20.0, idempotency_key="k1", status="SUBMITTED",
    )
    res = orch.run_daily_cycle()
    assert res["status"] == "BLOCKED_RECONCILIATION"
    run = db.get_trading_run(res["run_id"])
    assert run["status"] == "FAILED" and "Reconciliation not clean" in run["error_message"]
    assert db.get_active_trading_run() is None  # lease always released


def test_failed_cycle_releases_lease_and_records_failure(db, monkeypatch):
    orch = TradingDeskOrchestrator(db_path=db.db_path, universe=["SPY"])
    monkeypatch.setattr(orch, "_execute_cycle", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        orch.run_daily_cycle()
    last = db.get_last_trading_run()
    assert last["status"] == "FAILED" and "boom" in last["error_message"]
    assert db.get_active_trading_run() is None

def test_main_cli(monkeypatch):
    import sys
    from src.main import main
    from unittest.mock import patch, MagicMock
    
    with patch("src.main.TradingDeskOrchestrator") as mock_orch, \
         patch("src.main.subprocess.run") as mock_sub, \
         patch("src.main.Tier1VectorizedBacktester") as mock_t1, \
         patch("src.main.Tier2HistoricalReplayEngine") as mock_t2, \
         patch("src.main.MarketDataPipeline") as mock_mdp:
         
        mock_instance = MagicMock()
        mock_instance.run_daily_cycle.return_value = {"status": "SUCCESS"}
        mock_orch.return_value = mock_instance
        
        mock_res1 = MagicMock()
        mock_res1.total_trades = 10
        mock_res1.win_rate = 0.6
        mock_res1.profit_factor = 1.5
        mock_res1.expectancy_r = 0.5
        mock_res1.sharpe_ratio = 1.2
        mock_res1.sortino_ratio = 1.5
        mock_res1.max_drawdown_pct = 10.0
        mock_res1.final_equity = 110.0
        mock_res1.circuit_breaker_breaches = 0
        mock_t1.return_value.run.return_value = mock_res1
        
        mock_res2 = MagicMock()
        mock_res2.regime_name = "BULL"
        mock_res2.start_date = "2020"
        mock_res2.end_date = "2021"
        mock_res2.total_trades = 10
        mock_res2.win_rate = 0.6
        mock_res2.profit_factor = 1.5
        mock_res2.max_drawdown_pct = 10.0
        mock_res2.final_equity = 110.0
        mock_t2.return_value.run_replay.return_value = mock_res2
        
        # mock sleep
        with patch("src.main.time.sleep", side_effect=KeyboardInterrupt):
            monkeypatch.setattr(sys, "argv", ["main.py", "--loop"])
            try:
                main()
            except KeyboardInterrupt:
                pass
            
            monkeypatch.setattr(sys, "argv", ["main.py", "--run-once"])
            main()
            
            monkeypatch.setattr(sys, "argv", ["main.py", "--dashboard"])
            main()
            mock_sub.assert_called_once()
            
            monkeypatch.setattr(sys, "argv", ["main.py", "--backtest-tier1"])
            main()
            mock_t1.assert_called_once()
            
            monkeypatch.setattr(sys, "argv", ["main.py", "--backtest-tier2"])
            main()
            mock_t2.assert_called_once()
