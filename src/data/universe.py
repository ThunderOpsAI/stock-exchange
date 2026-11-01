"""
Asset Universe Catalog.
Provides structured, categorized universe selections:
- Core 11 liquid benchmark universe (Default conservative swing desk)
- Mega-Cap Tech & Semiconductors
- High-Growth Cloud / SaaS / AI Innovators & Startups
- High-Beta, Space, Frontier Fintech & Robotics
- Biotech, Genomics & MedTech
- Liquid Index & Thematic Sector ETFs
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set

CORE_UNIVERSE: List[str] = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "XLK", "SMH"
]

MEGACAP_AND_SEMIS: List[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "QCOM",
    "INTC", "ASML", "TSM", "TXN", "AMAT", "LRCX", "MU", "KLAC", "SNPS", "CDNS",
    "MRVL", "ARM", "SMCI", "ON", "ADI", "MPWR", "NXPI", "CRWD", "PANW", "FTNT"
]

AI_CLOUD_SAAS_STARTUPS: List[str] = [
    "PLTR", "SNOW", "DDOG", "NET", "MDB", "TEAM", "SHOP", "SQ", "COIN", "HOOD",
    "PATH", "APP", "DUOL", "TOST", "MNDY", "IOT", "CFLT", "GTLB", "AI", "BBAI",
    "SOUN", "STEM", "SYM", "NOW", "INTU", "ORCL", "ADBE", "CRM", "ZS", "OKTA",
    "ESTC", "DOCU", "TWLO", "DOCN", "EXAS", "TEM", "WDAY", "HUBS", "U", "ALTR"
]

HIGH_BETA_SPACE_FINTECH_ROBOTICS: List[str] = [
    "ASTS", "RKLB", "IONQ", "RGTI", "QBTS", "LUNR", "PL", "KTOS", "AVAV", "RIVN",
    "LCID", "NIO", "JOBY", "ACHR", "SOFI", "AFRM", "UPST", "NU", "OPEN", "RDFN",
    "CLOV", "CVNA", "CART", "KSS", "MARA", "RIOT", "CLSK", "CIFR", "BITF", "WULF",
    "CORZ", "CAN", "HIMS", "ENVX", "QS"
]

BIOTECH_GENOMICS_MEDTECH: List[str] = [
    "LLY", "NVO", "VRTX", "REGN", "ISRG", "MRNA", "BNTX", "CRSP", "NTLA", "BEAM",
    "PACB", "DNA", "RXRX", "TXG", "ILMN", "ALNY", "BMRN", "EXEL", "INCY", "SGEN",
    "BIIB", "GILD", "AMGN", "ARDX", "KROS"
]

LIQUID_ETFS: List[str] = [
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLE", "XLF", "XLV", "XLI", "XLY",
    "XLP", "XLU", "XLRE", "XBI", "IBB", "SMH", "SOXX", "ARKK", "ARKG", "TQQQ",
    "SOXL", "UVXY", "LABU", "TNA", "FAS"
]

# Combined universe mapping
CATEGORY_MAP: Dict[str, List[str]] = {
    "core": CORE_UNIVERSE,
    "megacap_semis": MEGACAP_AND_SEMIS,
    "ai_cloud_saas": AI_CLOUD_SAAS_STARTUPS,
    "high_beta_growth": HIGH_BETA_SPACE_FINTECH_ROBOTICS,
    "biotech": BIOTECH_GENOMICS_MEDTECH,
    "etfs": LIQUID_ETFS,
}


def get_universe(preset: str = "all") -> List[str]:
    """
    Returns a deduplicated list of tickers for the specified preset:
    - 'core': Original 11 liquid large-cap leaders & ETFs.
    - 'startups': 75 high-growth AI/cloud and space/robotics/fintech innovators.
    - 'large_cap': 55 mega-cap tech, semis, and major index ETFs.
    - 'aggressive': 100+ high-beta, growth tech, space, biotech, and momentum innovators.
    - 'all': 160+ full catalog.
    """
    preset_lower = (preset or "all").lower()

    if preset_lower == "core":
        return list(dict.fromkeys(CORE_UNIVERSE))

    if preset_lower == "startups":
        combined = AI_CLOUD_SAAS_STARTUPS + HIGH_BETA_SPACE_FINTECH_ROBOTICS
        return list(dict.fromkeys(combined))

    if preset_lower == "large_cap":
        combined = MEGACAP_AND_SEMIS + LIQUID_ETFS
        return list(dict.fromkeys(combined))

    if preset_lower == "aggressive":
        combined = (
            AI_CLOUD_SAAS_STARTUPS
            + HIGH_BETA_SPACE_FINTECH_ROBOTICS
            + BIOTECH_GENOMICS_MEDTECH
            + ["NVDA", "AMD", "ARM", "SMCI", "TSLA", "ARKK", "TQQQ", "SOXL"]
        )
        return list(dict.fromkeys(combined))

    # Default 'all': Full deduplicated catalog
    all_tickers = (
        CORE_UNIVERSE
        + MEGACAP_AND_SEMIS
        + AI_CLOUD_SAAS_STARTUPS
        + HIGH_BETA_SPACE_FINTECH_ROBOTICS
        + BIOTECH_GENOMICS_MEDTECH
        + LIQUID_ETFS
    )
    return list(dict.fromkeys(all_tickers))


def get_ticker_category(ticker: str) -> str:
    """Returns the primary category for a given ticker."""
    t = ticker.upper()
    for cat_name, tickers in CATEGORY_MAP.items():
        if t in tickers:
            return cat_name
    return "other"
