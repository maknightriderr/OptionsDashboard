"""
Shared, pure option-analytics primitives.

Everything here is framework-free and deterministic so it can be unit-tested in
isolation and reused by *both* the dashboard and the signal engine (no
duplicated math). Functions take plain rows / DataFrames and return numbers or
small dataclasses — they never touch the database, the broker, or Streamlit.

Two families live here:
  * chain shaping + headline metrics (pivot, PCR, max pain, ATM), and
  * directional read-outs (support/resistance walls, OI build-up).

IMPORTANT: these are conventional market-structure heuristics, not statistically
calibrated predictors. The signal engine treats their agreement as evidence, not
proof. Nothing here is trading advice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd

# Columns produced by build_chain_dataframe, in display order.
CHAIN_COLUMNS: tuple[str, ...] = (
    "CE_OI", "CE_OI_CHG", "CE_VOL", "CE_LTP",
    "STRIKE",
    "PE_LTP", "PE_VOL", "PE_OI_CHG", "PE_OI",
)


# --------------------------------------------------------------------------- #
# Chain shaping + headline metrics
# --------------------------------------------------------------------------- #
def build_chain_dataframe(rows: list[dict]) -> pd.DataFrame:
    """Pivot latest-tick rows into the CE | STRIKE | PE layout."""
    if not rows:
        return pd.DataFrame(columns=CHAIN_COLUMNS)

    frame = pd.DataFrame(rows)
    calls = frame[frame["option_type"] == "CE"]
    puts = frame[frame["option_type"] == "PE"]

    ce = calls.set_index("strike")[["oi", "oi_change", "volume", "ltp"]]
    ce.columns = ["CE_OI", "CE_OI_CHG", "CE_VOL", "CE_LTP"]
    pe = puts.set_index("strike")[["ltp", "volume", "oi_change", "oi"]]
    pe.columns = ["PE_LTP", "PE_VOL", "PE_OI_CHG", "PE_OI"]

    chain = ce.join(pe, how="outer").fillna(0)
    chain.index.name = "STRIKE"
    chain = chain.reset_index().sort_values("STRIKE").reset_index(drop=True)
    for col in CHAIN_COLUMNS:
        if col not in chain.columns:
            chain[col] = 0
    return chain[list(CHAIN_COLUMNS)]


def compute_pcr(chain: pd.DataFrame) -> float | None:
    """Put-Call Ratio by open interest = total PE OI / total CE OI."""
    if chain.empty:
        return None
    total_ce = float(chain["CE_OI"].sum())
    total_pe = float(chain["PE_OI"].sum())
    if total_ce <= 0:
        return None
    return round(total_pe / total_ce, 3)


def find_atm_strike(spot: float | None, chain: pd.DataFrame) -> float | None:
    """Return the listed strike closest to spot."""
    if spot is None or chain.empty:
        return None
    strikes = chain["STRIKE"].to_numpy()
    idx = (abs(strikes - spot)).argmin()
    return float(strikes[idx])


def compute_max_pain(chain: pd.DataFrame) -> float | None:
    """
    Max-pain strike: the expiry price (among listed strikes) at which the total
    intrinsic value owed by writers is smallest.

        pain(S) = Σ CE_OI(K)·max(S-K,0) + Σ PE_OI(K)·max(K-S,0)
    """
    if chain.empty:
        return None
    strikes = chain["STRIKE"].to_numpy(dtype=float)
    ce_oi = chain["CE_OI"].to_numpy(dtype=float)
    pe_oi = chain["PE_OI"].to_numpy(dtype=float)
    if ce_oi.sum() <= 0 and pe_oi.sum() <= 0:
        return None

    best_strike: float | None = None
    best_pain = float("inf")
    for candidate in strikes:
        call_pain = (ce_oi * (candidate - strikes).clip(min=0)).sum()
        put_pain = (pe_oi * (strikes - candidate).clip(min=0)).sum()
        total = call_pain + put_pain
        if total < best_pain:
            best_pain = total
            best_strike = float(candidate)
    return best_strike


# --------------------------------------------------------------------------- #
# Support / resistance from OI walls
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SupportResistance:
    """OI-wall support/resistance read-out."""

    support: float | None        # strike with the largest put OI (a floor)
    resistance: float | None     # strike with the largest call OI (a ceiling)
    support_oi: int
    resistance_oi: int


def support_resistance(chain: pd.DataFrame) -> SupportResistance:
    """
    Identify the dominant OI walls.

    Convention: the strike carrying the most *put* OI tends to act as support
    (writers defending below), and the most *call* OI as resistance (writers
    defending above). This is structural, not predictive.
    """
    if chain.empty:
        return SupportResistance(None, None, 0, 0)
    pe_idx = chain["PE_OI"].astype(float).idxmax()
    ce_idx = chain["CE_OI"].astype(float).idxmax()
    return SupportResistance(
        support=float(chain.loc[pe_idx, "STRIKE"]),
        resistance=float(chain.loc[ce_idx, "STRIKE"]),
        support_oi=int(chain.loc[pe_idx, "PE_OI"]),
        resistance_oi=int(chain.loc[ce_idx, "CE_OI"]),
    )


# --------------------------------------------------------------------------- #
# OI build-up (needs price + OI change, i.e. two snapshots)
# --------------------------------------------------------------------------- #
class BuildupType(str, Enum):
    LONG_BUILDUP = "long_buildup"        # price up,   OI up   -> bullish
    SHORT_BUILDUP = "short_buildup"      # price down, OI up   -> bearish
    SHORT_COVERING = "short_covering"    # price up,   OI down -> bullish
    LONG_UNWINDING = "long_unwinding"    # price down, OI down -> bearish
    NEUTRAL = "neutral"


def classify_buildup(price_change: float, oi_change: float) -> BuildupType:
    """Classic four-quadrant OI build-up from price and OI deltas."""
    eps = 1e-9
    price_up = price_change > eps
    price_dn = price_change < -eps
    oi_up = oi_change > eps
    oi_dn = oi_change < -eps
    if price_up and oi_up:
        return BuildupType.LONG_BUILDUP
    if price_dn and oi_up:
        return BuildupType.SHORT_BUILDUP
    if price_up and oi_dn:
        return BuildupType.SHORT_COVERING
    if price_dn and oi_dn:
        return BuildupType.LONG_UNWINDING
    return BuildupType.NEUTRAL


@dataclass(frozen=True)
class OIChangeBreakdown:
    """Aggregate OI deltas between an earlier and the current chain snapshot."""

    ce_oi_change: int            # net change in total call OI
    pe_oi_change: int            # net change in total put OI
    # A positive bias means puts are being added faster than calls (supportive);
    # negative means calls being added faster (resistive). Heuristic only.
    bias: float


def oi_change_breakdown(
    current: pd.DataFrame, previous: pd.DataFrame | None
) -> OIChangeBreakdown:
    """Compare total CE/PE OI between two snapshots to gauge writer flow."""
    if current.empty or previous is None or previous.empty:
        return OIChangeBreakdown(0, 0, 0.0)
    ce_now, pe_now = float(current["CE_OI"].sum()), float(current["PE_OI"].sum())
    ce_prev, pe_prev = float(previous["CE_OI"].sum()), float(previous["PE_OI"].sum())
    d_ce = ce_now - ce_prev
    d_pe = pe_now - pe_prev
    denom = abs(d_ce) + abs(d_pe)
    bias = 0.0 if denom == 0 else round((d_pe - d_ce) / denom, 3)
    return OIChangeBreakdown(int(d_ce), int(d_pe), bias)
