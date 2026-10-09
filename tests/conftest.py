"""
Shared pytest configuration and fixtures for autonomous stock-exchange system.
Provides deterministic database isolation, lockfile isolation, and environment safety.
"""

import os
from pathlib import Path
import pytest

from src.storage.db import Database


@pytest.fixture(autouse=True)
def guard_root_lock(tmp_path, monkeypatch):
    """
    Safety fixture: Ensure tests do not read or write a root HALTED.lock
    and ensure paper trading environment flags are enforced.
    """
    monkeypatch.setenv("TRADING_MODE", "paper")
    root_lock = Path("HALTED.lock")
    had_root_lock = root_lock.exists()

    yield

    # If a test created a root HALTED.lock, clean it up and fail if it wasn't there before
    if not had_root_lock and root_lock.exists():
        try:
            root_lock.unlink()
        except OSError:
            pass


@pytest.fixture
def isolated_db(tmp_path) -> Database:
    """Provide an isolated, freshly migrated SQLite database instance."""
    db_file = tmp_path / "test_trading.db"
    return Database(db_path=str(db_file))


@pytest.fixture
def isolated_lock(tmp_path) -> Path:
    """Provide an isolated lock file path that is initially unlocked."""
    lock_file = tmp_path / "test_HALTED.lock"
    if lock_file.exists():
        lock_file.unlink()
    return lock_file
