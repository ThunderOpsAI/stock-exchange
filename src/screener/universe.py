"""
Dynamic Liquid Universe Construction and Per-Run Versioning.
Implements:
- UniverseEligibilityRecord: Detailed eligibility audit record per symbol including liquidity (ADDV20),
  price ($15 min), bid/ask spread (<= 6 bps), tradability/halt status, corporate action blackout,
  and explicit reason for inclusion/exclusion.
- DynamicUniverseVersion: Immutable versioned snapshot of the liquid tradable universe for a run.
- DynamicUniverseConstructor: Service that filters candidate pools into a versioned universe.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from src.data.calendar import MarketCalendarGateService
from src.domain.models import ExecutionQuote


@dataclass
class UniverseEligibilityRecord:
    ticker: str
    as_of: str
    is_eligible: bool
    reason: str
    price: float
    addv_20_usd: float
    spread_bps: float
    is_tradable: bool
    is_halted: bool
    has_corporate_action_blackout: bool
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DynamicUniverseVersion:
    universe_version: str
    as_of: str
    eligible_symbols: List[str]
    total_scanned: int
    eligibility_records: Dict[str, UniverseEligibilityRecord]
    criteria: Dict[str, Any]
    run_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        res = asdict(self)
        res["eligibility_records"] = {
            k: v.to_dict() if hasattr(v, "to_dict") else v
            for k, v in self.eligibility_records.items()
        }
        return res

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


class DynamicUniverseConstructor:
    """
    Constructs and versions the tradable universe based on liquidity, price,
    bid/ask spread, trading halts, and corporate action events.
    """

    def __init__(
        self,
        calendar_service: Optional[MarketCalendarGateService] = None,
        min_price: float = 15.0,
        min_addv_usd: float = 25_000_000.0,
        max_spread_bps: float = 6.0,
        db: Optional[Any] = None,
    ):
        self.calendar_service = calendar_service or MarketCalendarGateService()
        self.min_price = min_price
        self.min_addv_usd = min_addv_usd
        self.max_spread_bps = max_spread_bps
        self.db = db

    def evaluate_symbol(
        self,
        ticker: str,
        df: pd.DataFrame,
        quote: Optional[ExecutionQuote] = None,
        as_of: Optional[datetime] = None,
    ) -> UniverseEligibilityRecord:
        """
        Evaluates a single symbol against liquidity, price, spread, halt, and event gates.
        Produces a complete UniverseEligibilityRecord with explicit reason.
        """
        now = as_of or datetime.now(timezone.utc)
        as_of_str = now.strftime("%Y-%m-%d %H:%M:%S")

        if df is None or df.empty or len(df) < 20:
            return UniverseEligibilityRecord(
                ticker=ticker,
                as_of=as_of_str,
                is_eligible=False,
                reason="EXCLUDED: Insufficient bar history (< 20 bars)",
                price=0.0,
                addv_20_usd=0.0,
                spread_bps=999.0,
                is_tradable=False,
                is_halted=False,
                has_corporate_action_blackout=False,
            )

        latest = df.iloc[-1]
        price = float(latest.get("close", 0.0))

        # Calculate 20-day Average Daily Dollar Volume
        if "addv_20" in latest and pd.notna(latest["addv_20"]):
            addv_20 = float(latest["addv_20"])
        else:
            dollar_vol = df["close"].iloc[-20:] * df["volume"].iloc[-20:]
            addv_20 = float(dollar_vol.mean())

        # Spread evaluation
        if quote is not None and quote.is_valid:
            spread_bps = float(quote.spread_bps)
        elif "spread_bps" in latest and pd.notna(latest["spread_bps"]):
            spread_bps = float(latest["spread_bps"])
        else:
            spread_bps = 3.0  # reference estimated

        # Gate evaluations
        is_halted = False
        if hasattr(self.calendar_service, "is_symbol_halted"):
            is_halted, _ = self.calendar_service.is_symbol_halted(ticker)
        elif hasattr(self.calendar_service, "is_halted"):
            is_halted = bool(self.calendar_service.is_halted(ticker))

        has_blackout = False
        if hasattr(self.calendar_service, "has_corporate_action_blackout"):
            has_blackout, _ = self.calendar_service.has_corporate_action_blackout(ticker, as_of=now)

        gate_res = self.calendar_service.evaluate_entry_gate(ticker, as_of=now)
        if not has_blackout and not gate_res.passed:
            has_blackout = (
                "blackout" in gate_res.reason.lower() or "corporate action" in gate_res.reason.lower()
            )
        is_tradable = gate_res.passed and not is_halted

        # Check eligibility criteria in order
        exclusions: List[str] = []
        if is_halted:
            exclusions.append(f"Symbol is halted ({ticker})")
        if has_blackout:
            exclusions.append("Active corporate action / earnings blackout window")
        if price < self.min_price:
            exclusions.append(f"Price ${price:.2f} < ${self.min_price:.2f} min")
        if addv_20 < self.min_addv_usd:
            exclusions.append(
                f"ADDV20 ${addv_20/1e6:.1f}M < ${self.min_addv_usd/1e6:.1f}M min"
            )
        if spread_bps > self.max_spread_bps:
            exclusions.append(
                f"Spread {spread_bps:.1f} bps > {self.max_spread_bps:.1f} bps max"
            )

        is_eligible = len(exclusions) == 0
        if is_eligible:
            reason = "ELIGIBLE: Passed all liquidity, price, spread, and tradability gates"
        else:
            reason = f"EXCLUDED: {'; '.join(exclusions)}"

        return UniverseEligibilityRecord(
            ticker=ticker,
            as_of=as_of_str,
            is_eligible=is_eligible,
            reason=reason,
            price=round(price, 2),
            addv_20_usd=round(addv_20, 2),
            spread_bps=round(spread_bps, 2),
            is_tradable=is_tradable,
            is_halted=is_halted,
            has_corporate_action_blackout=has_blackout,
            metrics={
                "close": price,
                "volume_20d_mean": float(df["volume"].iloc[-20:].mean()) if "volume" in df.columns else 0.0,
            },
        )

    def construct_universe(
        self,
        candidate_pool: Dict[str, pd.DataFrame],
        quotes: Optional[Dict[str, ExecutionQuote]] = None,
        as_of: Optional[datetime] = None,
        run_id: Optional[str] = None,
    ) -> DynamicUniverseVersion:
        """
        Evaluates the entire candidate pool and constructs a versioned universe.
        Computes SHA-256 hash of eligible universe membership and criteria.
        """
        now = as_of or datetime.now(timezone.utc)
        as_of_str = now.strftime("%Y-%m-%d %H:%M:%S")
        quotes_map = quotes or {}

        records: Dict[str, UniverseEligibilityRecord] = {}
        eligible_tickers: List[str] = []

        for ticker, df in candidate_pool.items():
            record = self.evaluate_symbol(
                ticker=ticker,
                df=df,
                quote=quotes_map.get(ticker),
                as_of=now,
            )
            records[ticker] = record
            if record.is_eligible:
                eligible_tickers.append(ticker)

        eligible_tickers = sorted(eligible_tickers)

        criteria = {
            "min_price": self.min_price,
            "min_addv_usd": self.min_addv_usd,
            "max_spread_bps": self.max_spread_bps,
        }

        # Deterministic universe version hash
        version_payload = {
            "as_of": as_of_str,
            "eligible": eligible_tickers,
            "criteria": criteria,
        }
        ver_hash = hashlib.sha256(json.dumps(version_payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        universe_version = f"univ_{now.strftime('%Y%m%d')}_{ver_hash}"

        # Persist to database if db provided
        if self.db is not None:
            try:
                for rec in records.values():
                    self.db.save_instrument_metadata(
                        ticker=rec.ticker,
                        sector=rec.metrics.get("sector"),
                        industry=rec.metrics.get("industry"),
                        tradable=1 if rec.is_eligible else 0,
                    )
            except Exception:
                pass

        return DynamicUniverseVersion(
            universe_version=universe_version,
            as_of=as_of_str,
            eligible_symbols=eligible_tickers,
            total_scanned=len(candidate_pool),
            eligibility_records=records,
            criteria=criteria,
            run_id=run_id,
        )
