"""
Unit tests for DataManifest, data provenance, and reproducible backtesting reports.
Verifies:
- Every backtest report points to a reproducible manifest and code/config version.
- Cryptographic SHA-256 checksums detect data tampering.
- Serialization and deserialization preserve full data provenance.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.backtest.manifest import DataManifest, get_current_git_version
from src.backtest.tier1_vectorized import Tier1VectorizedBacktester
from src.backtest.tier2_replay import Tier2HistoricalReplayEngine
from src.llm.committee import CacheMode
from src.storage.db import Database


def make_dummy_df(num_bars: int = 30, base: float = 100.0, seed: int = 42) -> pd.DataFrame:
    np.random.seed(seed)
    dates = [
        datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
        for i in range(num_bars)
    ]
    closes = np.linspace(base, base + 10.0, num_bars)
    highs = closes + 1.0
    lows = closes - 1.0
    opens = closes - 0.2
    vols = np.full(num_bars, 1_000_000.0)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=dates,
    )


def test_data_manifest_generation():
    df_aapl = make_dummy_df(base=150.0, seed=1)
    df_msft = make_dummy_df(base=300.0, seed=2)
    universe = {"AAPL": df_aapl, "MSFT": df_msft}

    manifest = DataManifest.from_universe(
        universe_dfs=universe,
        source="unit_test_synthetic",
        assumptions={"test_param": "alpha"},
    )

    assert manifest.manifest_id.startswith("mf_")
    assert manifest.version == "1.0.0"
    assert manifest.source == "unit_test_synthetic"
    assert manifest.universe_membership == ["AAPL", "MSFT"]
    assert "AAPL" in manifest.data_hashes
    assert "MSFT" in manifest.data_hashes
    assert len(manifest.data_hashes["AAPL"]) == 64  # SHA-256
    assert manifest.assumptions["test_param"] == "alpha"
    assert manifest.assumptions["initial_capital_usd"] == 100.0
    assert "commit_sha" in manifest.code_version


def test_manifest_integrity_verification_success_and_tampering():
    df_aapl = make_dummy_df(base=150.0, seed=1)
    df_msft = make_dummy_df(base=300.0, seed=2)
    universe = {"AAPL": df_aapl, "MSFT": df_msft}

    manifest = DataManifest.from_universe(universe_dfs=universe)

    # 1. Success check
    is_valid, errors = manifest.verify_data_integrity(universe)
    assert is_valid is True
    assert len(errors) == 0

    # 2. Tampered price value
    df_tampered = df_aapl.copy()
    df_tampered.iloc[5, df_tampered.columns.get_loc("close")] += 50.0  # modify close price
    tampered_universe = {"AAPL": df_tampered, "MSFT": df_msft}

    is_valid_tampered, errors_tampered = manifest.verify_data_integrity(tampered_universe)
    assert is_valid_tampered is False
    assert any("Checksum mismatch for AAPL" in e for e in errors_tampered)

    # 3. Missing symbol
    missing_universe = {"AAPL": df_aapl}
    is_valid_missing, errors_missing = manifest.verify_data_integrity(missing_universe)
    assert is_valid_missing is False
    assert any("Missing symbol in provided universe: MSFT" in e for e in errors_missing)


def test_manifest_serialization_and_deserialization():
    universe = {"AAPL": make_dummy_df(base=150.0), "TSLA": make_dummy_df(base=200.0)}
    manifest = DataManifest.from_universe(universe, corporate_actions=[{"type": "split", "ticker": "TSLA", "ratio": "3:1"}])

    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)

    try:
        manifest.save(path)
        loaded = DataManifest.load(path)

        assert loaded.manifest_id == manifest.manifest_id
        assert loaded.universe_membership == manifest.universe_membership
        assert loaded.data_hashes == manifest.data_hashes
        assert loaded.corporate_actions == manifest.corporate_actions
        assert loaded.assumptions == manifest.assumptions
        assert loaded.code_version == manifest.code_version
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_tier1_backtest_attaches_manifest():
    universe = {
        "AAPL": make_dummy_df(num_bars=100, base=150.0, seed=1),
        "NVDA": make_dummy_df(num_bars=100, base=120.0, seed=2),
    }
    spy = make_dummy_df(num_bars=100, base=500.0, seed=3)

    backtester = Tier1VectorizedBacktester(initial_capital=100.0)
    result = backtester.run(universe_dfs=universe, spy_df=spy)

    # Acceptance criteria: Every backtest report points to a reproducible manifest and code/config version
    assert result.manifest_id is not None
    assert result.manifest_id.startswith("mf_")
    assert result.manifest is not None
    assert result.manifest.universe_membership == ["AAPL", "NVDA"]
    assert result.code_version is not None
    assert "commit_sha" in result.code_version

    # Verify providing tampered data with explicit manifest fails
    tampered_universe = {
        "AAPL": make_dummy_df(num_bars=100, base=999.0, seed=99),
        "NVDA": universe["NVDA"],
    }
    with pytest.raises(ValueError) as exc_info:
        backtester.run(universe_dfs=tampered_universe, spy_df=spy, manifest=result.manifest)
    assert "Data manifest integrity verification failed" in str(exc_info.value)


def test_tier2_replay_attaches_manifest():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(db_path=path)

    try:
        universe = {
            "AAPL": make_dummy_df(num_bars=80, base=150.0, seed=1),
        }
        spy = make_dummy_df(num_bars=80, base=500.0, seed=2)

        engine = Tier2HistoricalReplayEngine(db=db, cache_mode=CacheMode.SYNTHETIC_MOCK)
        report = engine.run_replay(universe_dfs=universe, spy_df=spy, regime_name="Test Stress Replay")

        assert report.manifest_id is not None
        assert report.manifest is not None
        assert report.manifest.universe_membership == ["AAPL"]
        assert report.code_version is not None
    finally:
        if os.path.exists(path):
            os.remove(path)
