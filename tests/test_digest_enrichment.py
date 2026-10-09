"""
Unit and integration tests for digest enrichment and context gating.
Covers Ticket 30 (P6-02):
- Enrich digest with real earnings, filings, news provenance, macro events, and portfolio context.
- Every input carries timestamp/source.
- Unavailable required context blocks the affected decision (fail closed, ADR 0002).
"""

from datetime import datetime, timezone
import json
import pandas as pd
import pytest

from src.domain.models import CandidateStatus, ScreenedCandidate, StrategyType, VerdictOutcome
from src.llm.committee import TriadLLMCommittee
from src.llm.digest import ContextItem, DigestEnrichmentService, EnrichedDigest


def _create_sample_candidate() -> ScreenedCandidate:
    return ScreenedCandidate(
        candidate_id="cand_aapl_001",
        ticker="AAPL",
        strategy=StrategyType.TREND_PULLBACK,
        entry_est=150.0,
        stop_loss=147.0,
        take_profit=156.0,
        risk_r=3.0,
        allocated_usd=30.0,
        rank_score=92.5,
        status=CandidateStatus.PENDING_DELIBERATION,
        timestamp=datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc),
    )


def test_context_item_carries_timestamp_source_and_hash():
    item = ContextItem(
        source="SEC_EDGAR",
        timestamp="2026-06-10 12:00:00 UTC",
        data_type="regulatory_filing",
        payload={"form": "10-Q", "accession": "0000320193-26-000050"},
    )
    assert item.source == "SEC_EDGAR"
    assert "2026-06-10" in item.timestamp
    assert item.data_type == "regulatory_filing"
    assert len(item.compute_hash()) == 12

    d = item.to_dict()
    assert d["hash"] == item.compute_hash()
    assert d["source"] == "SEC_EDGAR"


def test_digest_enrichment_all_contexts_present():
    service = DigestEnrichmentService()
    candidate = _create_sample_candidate()

    df = pd.DataFrame(
        {
            "close": [148.0, 149.0, 150.0],
            "rsi_14": [45.0, 48.0, 52.0],
            "ema_20": [147.0, 148.0, 149.0],
            "sma_50": [145.0, 145.5, 146.0],
            "sma_200": [140.0, 140.5, 141.0],
            "atr_14": [2.5, 2.6, 2.5],
            "rvol_20": [1.1, 1.2, 1.3],
            "spread_bps": [2.5, 2.5, 2.4],
        }
    )

    earnings = {
        "source": "EARNINGS_WHISPERS",
        "timestamp": "2026-06-10 08:00:00 UTC",
        "next_earnings_date": "2026-07-28",
        "days_until_earnings": 48,
        "last_surprise_pct": 4.2,
    }

    macro = {
        "source": "FRED_MACRO_FEED",
        "timestamp": "2026-06-10 09:00:00 UTC",
        "regime": "BULLISH",
        "spy_close": 530.0,
        "spy_sma200": 500.0,
        "upcoming_fomc_days": 14,
    }

    portfolio = {
        "source": "RISK_SNAPSHOT",
        "timestamp": "2026-06-10 13:59:00 UTC",
        "current_equity_usd": 100.0,
        "cash_available_usd": 70.0,
        "occupied_slots": 1,
        "max_slots": 3,
        "sector_exposure_pct": 25.0,
    }

    filings = [{"source": "SEC_EDGAR", "form": "8-K", "title": "Item 2.02 Results of Operations"}]
    news = [{"source": "REUTERS", "title": "Apple expands enterprise AI offerings"}]

    digest = service.enrich(
        candidate=candidate,
        df=df,
        earnings_data=earnings,
        macro_data=macro,
        portfolio_data=portfolio,
        filings_data=filings,
        news_data=news,
    )

    assert isinstance(digest, EnrichedDigest)
    assert digest.is_complete is True
    assert digest.blocking_reason is None
    assert len(digest.missing_required_context) == 0

    # Every required context carries source & timestamp
    assert digest.price_context.source == "MARKET_DATA_PIPELINE"
    assert digest.earnings_context.source == "EARNINGS_WHISPERS"
    assert digest.macro_context.source == "FRED_MACRO_FEED"
    assert digest.portfolio_context.source == "RISK_SNAPSHOT"

    # Verify compact serialization
    compact_json = digest.to_compact_json()
    assert len(compact_json) < 2500  # Well within 1,000 tokens


def test_digest_enrichment_missing_required_context_blocks_deliberation():
    service = DigestEnrichmentService()
    candidate = _create_sample_candidate()

    # Omit earnings_data and macro_data
    df = pd.DataFrame({"close": [150.0], "rsi_14": [50.0]})
    portfolio = {"current_equity_usd": 100.0}

    digest = service.enrich(
        candidate=candidate,
        df=df,
        earnings_data=None,  # Missing!
        macro_data=None,     # Missing!
        portfolio_data=portfolio,
    )

    assert digest.is_complete is False
    assert "earnings_context" in digest.missing_required_context
    assert "macro_context" in digest.missing_required_context
    assert "FAIL_CLOSED" in digest.blocking_reason

    # Test deliberation gating with incomplete digest
    committee = TriadLLMCommittee()
    verdict, deliberations = committee.deliberate(
        candidate=candidate,
        df=df,
        enriched_digest=digest,
    )

    assert verdict.verdict_outcome == VerdictOutcome.AUTO_DROPPED
    assert verdict.composite_score == 0.0
    assert verdict.risk_officer_dissent is True
    assert "blocked" in verdict.verdict_id.lower()
    assert len(deliberations) == 0
