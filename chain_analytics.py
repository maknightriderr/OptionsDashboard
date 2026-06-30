"""
Chain-level analytics built on the Black-76 primitives.

Takes an option-chain snapshot (the CE | STRIKE | PE DataFrame) plus spot, and
produces:
  * per-strike implied vol and Greeks for both sides (the IV surface / smile),
  * gamma exposure (GEX) per strike, the net, and an approximate flip strike,
  * ATM implied vol, and
  * IV rank / percentile from a supplied history.

Everything is pure and unit-tested. IV is reported in percent (15.2 = 15.2%);
Greeks are in the units documented in analytics.greeks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from analytics import greeks as bs
from database.models import OptionType

_IST = ZoneInfo("Asia/Kolkata")
_EXPIRY_TIME_IST = time(15, 30)   # index options expire at 15:30 IST


def time_to_expiry(expiry_iso: str, now: datetime | None = None) -> float:
    """Years to expiry, measuring to 15:30 IST on the expiry date."""
    now = now or datetime.now(tz=timezone.utc)
    expiry_date = datetime.fromisoformat(expiry_iso).date()
    expiry_dt = datetime.combine(expiry_date, _EXPIRY_TIME_IST, tzinfo=_IST)
    seconds = (expiry_dt - now).total_seconds()
    return max(seconds / (365.25 * 24 * 3600), bs._MIN_T)


# Per-side display columns produced by compute_chain_greeks.
GREEK_COLUMNS: tuple[str, ...] = (
    "STRIKE",
    "CE_IV", "CE_DELTA", "CE_GAMMA", "CE_VEGA", "CE_THETA",
    "PE_IV", "PE_DELTA", "PE_GAMMA", "PE_VEGA", "PE_THETA",
    "CE_OI", "PE_OI",
)


def compute_chain_greeks(
    chain: pd.DataFrame, spot: float, r: float, q: float, expiry_iso: str,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Solve IV and compute Greeks for every strike on both sides."""
    if chain.empty or spot <= 0:
        return pd.DataFrame(columns=GREEK_COLUMNS)

    t = time_to_expiry(expiry_iso, now)
    f = bs.forward_price(spot, r, q, t)
    records: list[dict] = []

    for _, row in chain.iterrows():
        k = float(row["STRIKE"])
        rec: dict = {"STRIKE": k, "CE_OI": int(row.get("CE_OI", 0)), "PE_OI": int(row.get("PE_OI", 0))}
        for side, ot in (("CE", OptionType.CALL), ("PE", OptionType.PUT)):
            ltp = float(row.get(f"{side}_LTP", 0.0))
            iv = bs.implied_vol(ltp, f, k, t, r, ot) if ltp > 0 else None
            if iv is not None:
                gk = bs.compute_greeks(f, k, t, iv, r, q, ot)
                rec[f"{side}_IV"] = round(iv * 100.0, 2)
                rec[f"{side}_DELTA"] = round(gk.delta, 4)
                rec[f"{side}_GAMMA"] = round(gk.gamma, 6)
                rec[f"{side}_VEGA"] = round(gk.vega, 4)
                rec[f"{side}_THETA"] = round(gk.theta, 4)
            else:
                for suffix in ("IV", "DELTA", "GAMMA", "VEGA", "THETA"):
                    rec[f"{side}_{suffix}"] = None
        records.append(rec)

    frame = pd.DataFrame(records)
    for col in GREEK_COLUMNS:
        if col not in frame.columns:
            frame[col] = None
    return frame[list(GREEK_COLUMNS)].sort_values("STRIKE").reset_index(drop=True)


@dataclass(frozen=True)
class IVSmile:
    strikes: list[float]
    ce_iv: list[float | None]
    pe_iv: list[float | None]


def iv_smile(chain_greeks: pd.DataFrame) -> IVSmile:
    """Extract the strike/IV smile for plotting."""
    if chain_greeks.empty:
        return IVSmile([], [], [])
    return IVSmile(
        strikes=[float(s) for s in chain_greeks["STRIKE"]],
        ce_iv=[None if pd.isna(v) else float(v) for v in chain_greeks["CE_IV"]],
        pe_iv=[None if pd.isna(v) else float(v) for v in chain_greeks["PE_IV"]],
    )


def atm_iv(chain_greeks: pd.DataFrame, atm_strike: float | None) -> float | None:
    """Average of CE/PE IV at the ATM strike (whichever are available)."""
    if chain_greeks.empty or atm_strike is None:
        return None
    row = chain_greeks[chain_greeks["STRIKE"] == atm_strike]
    if row.empty:
        return None
    vals = [row.iloc[0]["CE_IV"], row.iloc[0]["PE_IV"]]
    vals = [float(v) for v in vals if v is not None and not pd.isna(v)]
    return round(sum(vals) / len(vals), 2) if vals else None


@dataclass(frozen=True)
class GammaExposure:
    per_strike: pd.DataFrame      # STRIKE, GEX
    total: float
    flip_strike: float | None     # approximate zero-gamma strike


def gamma_exposure(
    chain_greeks: pd.DataFrame, spot: float, contract_size: float = 1.0
) -> GammaExposure:
    """
    Dealer gamma exposure per strike (SqueezeMetrics-style convention:
    dealers long calls, short puts), scaled per 1% spot move:

        GEX(K) = (CE_gamma·CE_OI - PE_gamma·PE_OI) · contract_size · spot² · 0.01

    The flip strike is the approximate price where cumulative GEX crosses zero.
    Multiply by the index lot size (contract_size) for notional units.
    """
    if chain_greeks.empty or spot <= 0:
        return GammaExposure(pd.DataFrame(columns=["STRIKE", "GEX"]), 0.0, None)

    scale = contract_size * spot * spot * 0.01
    rows = []
    for _, r in chain_greeks.iterrows():
        cg = r["CE_GAMMA"] if r["CE_GAMMA"] is not None and not pd.isna(r["CE_GAMMA"]) else 0.0
        pg = r["PE_GAMMA"] if r["PE_GAMMA"] is not None and not pd.isna(r["PE_GAMMA"]) else 0.0
        gex = (float(cg) * int(r["CE_OI"]) - float(pg) * int(r["PE_OI"])) * scale
        rows.append({"STRIKE": float(r["STRIKE"]), "GEX": gex})

    per_strike = pd.DataFrame(rows)
    total = float(per_strike["GEX"].sum())

    # Approximate flip: first strike where the cumulative GEX changes sign.
    flip: float | None = None
    cumulative = 0.0
    prev_sign = 0
    for _, r in per_strike.iterrows():
        cumulative += r["GEX"]
        sign = 1 if cumulative > 0 else -1 if cumulative < 0 else 0
        if prev_sign and sign and sign != prev_sign:
            flip = float(r["STRIKE"])
            break
        prev_sign = sign or prev_sign
    return GammaExposure(per_strike=per_strike, total=total, flip_strike=flip)


# --------------------------------------------------------------------------- #
# IV rank / percentile (need a history of ATM IV)
# --------------------------------------------------------------------------- #
def iv_rank(current_iv: float, history: list[float]) -> float | None:
    """IV Rank = (current - min) / (max - min) * 100 over the history window."""
    if not history:
        return None
    lo, hi = min(history), max(history)
    if hi <= lo:
        return None
    return round((current_iv - lo) / (hi - lo) * 100.0, 1)


def iv_percentile(current_iv: float, history: list[float]) -> float | None:
    """IV Percentile = share of history strictly below current IV, as a percent."""
    if not history:
        return None
    below = sum(1 for v in history if v < current_iv)
    return round(below / len(history) * 100.0, 1)
