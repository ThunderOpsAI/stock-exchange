"""
Tests for asset universe catalog and categorization.
"""

from src.data.universe import (
    CATEGORY_MAP,
    CORE_UNIVERSE,
    get_ticker_category,
    get_universe,
)


def test_core_universe_preset():
    core = get_universe("core")
    assert len(core) == len(CORE_UNIVERSE)
    assert "SPY" in core
    assert "NVDA" in core


def test_startups_universe_preset():
    startups = get_universe("startups")
    assert len(startups) >= 70
    assert "PLTR" in startups
    assert "ASTS" in startups
    assert "IONQ" in startups
    assert "RKLB" in startups


def test_aggressive_universe_preset():
    aggressive = get_universe("aggressive")
    assert len(aggressive) >= 100
    assert "PLTR" in aggressive
    assert "ASTS" in aggressive
    assert "CRSP" in aggressive
    assert "SOXL" in aggressive


def test_all_universe_preset():
    all_tickers = get_universe("all")
    assert len(all_tickers) >= 150
    # No duplicates
    assert len(all_tickers) == len(set(all_tickers))


def test_get_ticker_category():
    assert get_ticker_category("NVDA") in ["core", "megacap_semis"]
    assert get_ticker_category("PLTR") == "ai_cloud_saas"
    assert get_ticker_category("ASTS") == "high_beta_growth"
    assert get_ticker_category("CRSP") == "biotech"
    assert get_ticker_category("QQQ") in ["core", "etfs"]
    assert get_ticker_category("UNKNOWN123") == "other"
