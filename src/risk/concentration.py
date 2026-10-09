"""
Concentration & Correlation Risk Manager (P3-04).
Enforces sector caps, pairwise correlation thresholds, and event-risk exclusions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from pydantic import BaseModel

from src.broker.base import AbstractBrokerAdapter
from src.domain.models import AuditSeverity, Position
from src.storage.db import Database

logger = logging.getLogger(__name__)


class ConcentrationRiskLimits(BaseModel):
    max_sector_slots: int = 1  # In a 3-slot portfolio, at most 1 position per sector
    max_sector_exposure_pct: float = 0.50  # Max 50% of portfolio in any single sector
    max_correlation_threshold: float = 0.85  # Pairwise correlation ceiling
    earnings_blackout_days: int = 7  # Days before earnings to blackout entries


class ConcentrationRiskManager:
    def __init__(
        self,
        db: Database,
        broker: AbstractBrokerAdapter,
        limits: Optional[ConcentrationRiskLimits] = None,
    ):
        self.db = db
        self.broker = broker
        self.limits = limits or ConcentrationRiskLimits()

    def get_ticker_sector(self, ticker: str) -> str:
        """Retrieves sector from instrument_metadata or defaults to 'UNKNOWN'."""
        meta = self.db.get_instrument_metadata(ticker.upper())
        if meta and meta.get("sector"):
            return str(meta["sector"]).strip().upper()
        return "UNKNOWN"

    def evaluate_concentration(
        self,
        candidate_ticker: str,
        allocated_usd: float = 30.0,
        candidate_returns: Optional[pd.Series] = None,
        open_position_returns: Optional[Dict[str, pd.Series]] = None,
        as_of: Optional[datetime] = None,
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Evaluates candidate against:
        1. Event risk (corporate action flag, earnings within 7 days)
        2. Sector concentration cap (slots and notional)
        3. Pairwise correlation threshold (> 0.85) against existing open positions
        """
        now = as_of or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        cand_upper = candidate_ticker.upper()
        meta = self.db.get_instrument_metadata(cand_upper)

        # 1. Event risk check (earnings & corporate action)
        if meta:
            if meta.get("corporate_action_flag", 0) == 1:
                reason = f"EVENT_RISK_EXCLUSION: {cand_upper} has active corporate action flag"
                self._log_decision(False, reason, {"ticker": cand_upper, "event": "corporate_action"})
                return False, reason, {"ticker": cand_upper, "event": "corporate_action"}

            next_earnings = meta.get("next_earnings_date")
            if next_earnings:
                try:
                    earn_dt = (
                        datetime.fromisoformat(next_earnings)
                        if isinstance(next_earnings, str)
                        else next_earnings
                    )
                    if earn_dt.tzinfo is None:
                        earn_dt = earn_dt.replace(tzinfo=timezone.utc)
                    days_to_earnings = (earn_dt - now).total_seconds() / 86400.0
                    if 0 <= days_to_earnings <= self.limits.earnings_blackout_days:
                        reason = (
                            f"EVENT_RISK_EXCLUSION: {cand_upper} earnings announcement in "
                            f"{days_to_earnings:.1f} days (blackout threshold: {self.limits.earnings_blackout_days}d)"
                        )
                        self._log_decision(False, reason, {"ticker": cand_upper, "days_to_earnings": days_to_earnings})
                        return False, reason, {"ticker": cand_upper, "days_to_earnings": days_to_earnings}
                except Exception as e:
                    logger.warning(f"Error parsing next_earnings_date for {cand_upper}: {e}")

        # 2. Sector concentration check
        open_positions = self.broker.get_positions()
        cand_sector = self.get_ticker_sector(cand_upper)

        if cand_sector != "UNKNOWN":
            sector_matches = 0
            for pos in open_positions:
                pos_sector = self.get_ticker_sector(pos.ticker)
                if pos_sector == cand_sector:
                    sector_matches += 1

            if sector_matches >= self.limits.max_sector_slots:
                reason = (
                    f"SECTOR_CONCENTRATION_EXCEEDED: Sector '{cand_sector}' already has "
                    f"{sector_matches} open position(s) (cap: {self.limits.max_sector_slots})"
                )
                self._log_decision(False, reason, {"ticker": cand_upper, "sector": cand_sector, "count": sector_matches})
                return False, reason, {"ticker": cand_upper, "sector": cand_sector, "count": sector_matches}

        # 3. Pairwise correlation check
        if candidate_returns is not None and open_position_returns:
            for pos in open_positions:
                pos_ret = open_position_returns.get(pos.ticker.upper())
                if pos_ret is not None and len(pos_ret) > 10 and len(candidate_returns) > 10:
                    corr = float(candidate_returns.corr(pos_ret))
                    if not pd.isna(corr) and corr >= self.limits.max_correlation_threshold:
                        reason = (
                            f"CORRELATION_CONCENTRATION_EXCEEDED: Correlation between {cand_upper} and "
                            f"{pos.ticker} is {corr:.2f} >= threshold {self.limits.max_correlation_threshold:.2f}"
                        )
                        self._log_decision(
                            False, reason, {"ticker": cand_upper, "correlated_with": pos.ticker, "corr": corr}
                        )
                        return (
                            False,
                            reason,
                            {"ticker": cand_upper, "correlated_with": pos.ticker, "corr": corr},
                        )

        self._log_decision(True, f"Concentration checks passed for {cand_upper}", {"ticker": cand_upper, "sector": cand_sector})
        return True, None, {"ticker": cand_upper, "sector": cand_sector}

    def _log_decision(self, allowed: bool, message: str, meta: Dict[str, Any]) -> None:
        self.db.save_audit_log(
            severity=AuditSeverity.INFO if allowed else AuditSeverity.WARNING,
            component="ConcentrationRisk",
            event_name="CONCENTRATION_CHECK",
            message=message,
            metadata=meta,
        )
