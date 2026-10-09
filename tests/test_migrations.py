"""
Unit and Integration tests for Ordered SQLite Migration Engine and Execution Tables.
Tests ADR 0003 migration contracts and SPEC.md Phase 2 storage requirements.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import pytest

from src.storage.db import Database
from src.storage.migration_engine import MigrationEngine

pytestmark = pytest.mark.integration


@pytest.fixture
def clean_db_path(tmp_path):
    return str(tmp_path / "test_migration.db")


def test_fresh_db_migrations(clean_db_path):
    """Verifies that on a fresh DB, MigrationEngine applies all migrations in order."""
    engine = MigrationEngine(db_path=clean_db_path)
    applied = engine.run_migrations()

    assert applied == [1, 2]

    # Verify schema_migrations table
    conn = sqlite3.connect(clean_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT version, name FROM schema_migrations ORDER BY version ASC")
    rows = cursor.fetchall()
    assert len(rows) == 2
    assert rows[0][0] == 1
    assert rows[1][0] == 2

    # Verify newly added Phase 2 tables exist
    cursor.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='table' AND name IN (
            'trading_runs', 'order_intents', 'broker_submissions',
            'reconciliation_events', 'protection_status', 'instrument_metadata'
        )
        """
    )
    table_names = {r[0] for r in cursor.fetchall()}
    assert table_names == {
        "trading_runs",
        "order_intents",
        "broker_submissions",
        "reconciliation_events",
        "protection_status",
        "instrument_metadata",
    }
    conn.close()


def test_migration_idempotency(clean_db_path):
    """Verifies that running migrations multiple times is completely idempotent."""
    engine = MigrationEngine(db_path=clean_db_path)
    first_run = engine.run_migrations()
    assert first_run == [1, 2]

    second_run = engine.run_migrations()
    assert second_run == []  # No new migrations applied


def test_legacy_db_bootstrap_and_migration(clean_db_path):
    """
    Tests backward compatibility:
    If a database has tables from v1 but no schema_migrations table,
    MigrationEngine bootstraps v1 and applies v2 cleanly.
    """
    # Create legacy database using initial schema only
    v1_sql_path = Path("src/storage/migrations/0001_initial_schema.sql")
    with open(v1_sql_path, "r", encoding="utf-8") as f:
        v1_sql = f.read()

    conn = sqlite3.connect(clean_db_path)
    conn.executescript(v1_sql)
    conn.close()

    # Now run migrations via MigrationEngine
    engine = MigrationEngine(db_path=clean_db_path)
    applied = engine.run_migrations()

    # Should only apply migration 2
    assert applied == [2]

    conn = sqlite3.connect(clean_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT version FROM schema_migrations ORDER BY version ASC")
    versions = [r[0] for r in cursor.fetchall()]
    assert versions == [1, 2]
    conn.close()


def test_trading_runs_crud(clean_db_path):
    db = Database(db_path=clean_db_path)

    # 1. Create run
    run = db.create_trading_run(
        run_id="run_test_001",
        mode="paper",
        config_hash="abc123hash",
        trigger="cron_daily",
        lease_owner="worker_01",
    )
    assert run["run_id"] == "run_test_001"
    assert run["status"] == "STARTING"
    assert run["mode"] == "paper"

    # 2. Update to RUNNING
    db.update_trading_run_status("run_test_001", "RUNNING")
    active = db.get_active_trading_run()
    assert active is not None
    assert active["run_id"] == "run_test_001"
    assert active["status"] == "RUNNING"

    # 3. Complete run
    db.update_trading_run_status("run_test_001", "COMPLETED")
    updated = db.get_trading_run("run_test_001")
    assert updated["status"] == "COMPLETED"
    assert updated["ended_at"] is not None
    assert db.get_active_trading_run() is None


def test_order_intents_and_submissions(clean_db_path):
    db = Database(db_path=clean_db_path)

    intent_id = db.save_order_intent(
        intent_id="intent_001",
        request_hash="req_hash_xyz",
        ticker="AAPL",
        side="BUY",
        target_qty=0.25,
        allocated_usd=28.50,
        idempotency_key="idem_key_uuid",
        risk_decision="APPROVED",
        risk_reason="Passed all deterministic risk filters",
        limit_price=114.00,
        stop_loss=109.00,
        take_profit=124.00,
    )
    assert intent_id == "intent_001"

    # Query by idempotency key
    found = db.get_order_intent_by_idempotency_key("idem_key_uuid")
    assert found is not None
    assert found["ticker"] == "AAPL"
    assert found["status"] == "CREATED"

    # Record broker submission attempt
    sub_id = db.record_broker_submission(
        submission_id="sub_001",
        intent_id="intent_001",
        broker_order_id="broker_ord_999",
        attempt_number=1,
        client_order_id="client_ord_001",
        outcome_classification="ACCEPTED",
        request_payload_redacted='{"ticker": "AAPL", "qty": 0.25}',
        response_payload_redacted='{"status": "accepted"}',
    )
    assert sub_id == "sub_001"

    subs = db.get_submissions_for_intent("intent_001")
    assert len(subs) == 1
    assert subs[0]["outcome_classification"] == "ACCEPTED"

    # Update intent status
    db.update_order_intent_status("intent_001", "SUBMITTED")
    updated_intent = db.get_order_intent("intent_001")
    assert updated_intent["status"] == "SUBMITTED"


def test_reconciliation_events_and_protection_status(clean_db_path):
    db = Database(db_path=clean_db_path)

    # 1. Reconciliation event
    rev_id = db.record_reconciliation_event(
        event_id="rec_001",
        local_snapshot_hash="local_hash_1",
        broker_snapshot_hash="broker_hash_1",
        mismatches_json="[]",
        resolution_status="HEALTHY_MATCH",
        resolution_notes="Zero discrepancies detected on restart",
    )
    assert rev_id == "rec_001"

    latest_rec = db.get_latest_reconciliation_event()
    assert latest_rec is not None
    assert latest_rec["resolution_status"] == "HEALTHY_MATCH"

    # 2. Protection status
    prot_id = db.save_protection_status(
        protection_id="prot_001",
        ticker="AAPL",
        protection_mode="NATIVE_BRACKET",
        stop_loss_order_id="sl_leg_123",
        take_profit_order_id="tp_leg_456",
        watchdog_healthy=1,
    )
    assert prot_id == "prot_001"

    active_prots = db.get_active_protection_statuses()
    assert len(active_prots) == 1
    assert active_prots[0]["protection_mode"] == "NATIVE_BRACKET"


def test_instrument_metadata_crud(clean_db_path):
    db = Database(db_path=clean_db_path)

    db.save_instrument_metadata(
        ticker="MSFT",
        exchange="NASDAQ",
        universe_version="2026.1",
        sector="Technology",
        industry="Software",
        tradable=1,
        corporate_action_flag=0,
    )

    meta = db.get_instrument_metadata("MSFT")
    assert meta is not None
    assert meta["exchange"] == "NASDAQ"
    assert meta["sector"] == "Technology"
    assert meta["tradable"] == 1
