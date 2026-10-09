"""
Reproducible Backtest Data Manifests & Data Provenance.
Implements:
- DataManifest: Versioned dataset descriptor recording data source, retrieval timestamp,
  universe membership, cryptographic data hashes (SHA-256), corporate actions, and modeling assumptions.
- Code & configuration versioning (git commit, branch, config hash).
- Data integrity verification to guarantee exact backtest reproducibility.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd


def get_current_git_version() -> Dict[str, str]:
    """Retrieves current git commit sha and branch if inside git repository."""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
        ).decode("utf-8").strip()
    except Exception:
        commit = os.getenv("GIT_COMMIT_SHA", "unknown")

    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], stderr=subprocess.DEVNULL
        ).decode("utf-8").strip()
    except Exception:
        branch = os.getenv("GIT_BRANCH", "unknown")

    return {"commit_sha": commit, "branch": branch}


@dataclass
class DataManifest:
    manifest_id: str
    version: str = "1.0.0"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source: str = "daily_historical_bars"
    date_range: Dict[str, str] = field(default_factory=lambda: {"start": "", "end": ""})
    universe_membership: List[str] = field(default_factory=list)
    data_hashes: Dict[str, str] = field(default_factory=dict)
    corporate_actions: List[Dict[str, Any]] = field(default_factory=list)
    assumptions: Dict[str, Any] = field(default_factory=dict)
    code_version: Dict[str, str] = field(default_factory=get_current_git_version)

    @staticmethod
    def compute_dataframe_hash(df: pd.DataFrame) -> str:
        """Computes deterministic SHA-256 hash of a pandas DataFrame's values and index."""
        # Convert index and values to deterministic CSV bytes
        data_bytes = df.to_csv(index=True, date_format="%Y-%m-%d %H:%M:%S").encode("utf-8")
        return hashlib.sha256(data_bytes).hexdigest()

    @classmethod
    def from_universe(
        cls,
        universe_dfs: Dict[str, pd.DataFrame],
        source: str = "historical_bars_daily",
        assumptions: Optional[Dict[str, Any]] = None,
        corporate_actions: Optional[List[Dict[str, Any]]] = None,
        code_version: Optional[Dict[str, str]] = None,
    ) -> DataManifest:
        """
        Creates a reproducible DataManifest from an in-memory dictionary of universe DataFrames.
        Computes SHA-256 checksum for each ticker.
        """
        universe_membership = sorted(list(universe_dfs.keys()))
        data_hashes: Dict[str, str] = {}
        all_dates = set()

        for ticker, df in universe_dfs.items():
            data_hashes[ticker] = cls.compute_dataframe_hash(df)
            if not df.empty:
                all_dates.update(df.index)

        sorted_dates = sorted(list(all_dates))
        start_date = str(sorted_dates[0])[:10] if sorted_dates else ""
        end_date = str(sorted_dates[-1])[:10] if sorted_dates else ""

        base_assumptions = {
            "initial_capital_usd": 100.0,
            "max_slots": 3,
            "slot_target_usd": 30.0,
            "cash_buffer_usd": 10.0,
            "max_risk_cap_usd": 3.0,
            "half_spread_bps": 3.0,
            "execution_timing": "Open[t] upon Signal[t-1]",
            "anti_lookahead_enforced": True,
        }
        if assumptions:
            base_assumptions.update(assumptions)

        git_ver = code_version or get_current_git_version()

        # Compute composite manifest ID
        manifest_payload = {
            "source": source,
            "universe": universe_membership,
            "data_hashes": data_hashes,
            "date_range": {"start": start_date, "end": end_date},
            "assumptions": base_assumptions,
            "code_version": git_ver,
        }
        raw_manifest_str = json.dumps(manifest_payload, sort_keys=True)
        manifest_id = f"mf_{hashlib.sha256(raw_manifest_str.encode('utf-8')).hexdigest()[:16]}"

        return cls(
            manifest_id=manifest_id,
            version="1.0.0",
            created_at=datetime.now(timezone.utc).isoformat(),
            source=source,
            date_range={"start": start_date, "end": end_date},
            universe_membership=universe_membership,
            data_hashes=data_hashes,
            corporate_actions=corporate_actions or [],
            assumptions=base_assumptions,
            code_version=git_ver,
        )

    def verify_data_integrity(
        self, universe_dfs: Dict[str, pd.DataFrame]
    ) -> Tuple[bool, List[str]]:
        """
        Verifies that input DataFrames match the manifest's cryptographic checksums.
        Returns (is_valid, list_of_discrepancies).
        """
        discrepancies: List[str] = []
        for ticker in self.universe_membership:
            if ticker not in universe_dfs:
                discrepancies.append(f"Missing symbol in provided universe: {ticker}")
                continue

            current_hash = self.compute_dataframe_hash(universe_dfs[ticker])
            expected_hash = self.data_hashes.get(ticker)
            if current_hash != expected_hash:
                discrepancies.append(
                    f"Checksum mismatch for {ticker}: expected {expected_hash}, got {current_hash}"
                )

        # Check for unexpected extra symbols
        for ticker in universe_dfs.keys():
            if ticker not in self.data_hashes:
                discrepancies.append(f"Unexpected extra symbol in universe: {ticker}")

        return len(discrepancies) == 0, discrepancies

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def save(self, path: Union[str, Path]) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json())

    @classmethod
    def load(cls, path: Union[str, Path]) -> DataManifest:
        p = Path(path)
        data = json.loads(p.read_text())
        return cls(**data)
