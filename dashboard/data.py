"""
Dashboard data shaping.

The heavy lifting now lives in ``analytics.indicators`` so the signal engine and
the dashboard share one implementation (no duplicated math). This module
re-exports those primitives and keeps only the dashboard-specific summary bundle.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from analytics.indicators import (  # re-exported for backward compatibility
    CHAIN_COLUMNS,
    build_chain_dataframe,
    compute_max_pain,
    compute_pcr,
    find_atm_strike,
)

__all__ = [
    "CHAIN_COLUMNS",
    "ChainSummary",
    "build_chain_dataframe",
    "compute_max_pain",
    "compute_pcr",
    "find_atm_strike",
    "summarise_chain",
]


@dataclass(frozen=True)
class ChainSummary:
    """Top-line metrics derived from a chain snapshot (dashboard display)."""

    spot: float | None
    atm_strike: float | None
    total_ce_oi: int
    total_pe_oi: int
    pcr: float | None
    max_pain: float | None
    contracts: int
    last_update: str | None


def summarise_chain(
    chain: pd.DataFrame, spot: float | None, last_update: str | None
) -> ChainSummary:
    """Bundle the headline metrics the dashboard shows above the table."""
    return ChainSummary(
        spot=spot,
        atm_strike=find_atm_strike(spot, chain),
        total_ce_oi=int(chain["CE_OI"].sum()) if not chain.empty else 0,
        total_pe_oi=int(chain["PE_OI"].sum()) if not chain.empty else 0,
        pcr=compute_pcr(chain),
        max_pain=compute_max_pain(chain),
        contracts=int(len(chain) * 2) if not chain.empty else 0,
        last_update=last_update,
    )
