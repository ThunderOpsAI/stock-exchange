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
