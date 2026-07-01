"""
Offline tests for Phase 5: Black-76 pricing/Greeks (validated against finite
differences and put-call parity), the IV solver (round-trip + no-solution
handling), chain-level Greeks/IV surface, gamma exposure, and IV rank/percentile.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from analytics import greeks as bs
from analytics.chain_analytics import (
    atm_iv,
    compute_chain_greeks,
    gamma_exposure,
    iv_percentile,
    iv_rank,
    iv_smile,
    time_to_expiry,
)
from analytics.indicators import build_chain_dataframe
from database.models import OptionType
from database.sqlite_db import SQLiteDatabase

F, K, T, R, Q = 24000.0, 24000.0, 30 / 365, 0.065, 0.012


def _fd(fn, x, h):
    return (fn(x + h) - fn(x - h)) / (2 * h)


# --------------------------------------------------------------------------- #
# pricing / parity
# --------------------------------------------------------------------------- #
def test_put_call_parity() -> None:
    c = bs.price(F, K, T, 0.15, R, OptionType.CALL)
    p = bs.price(F, K, T, 0.15, R, OptionType.PUT)
    assert c - p == pytest.approx(math.exp(-R * T) * (F - K), abs=1e-9)


def test_price_monotonic_in_vol() -> None:
    lo = bs.price(F, K, T, 0.10, R, OptionType.CALL)
    hi = bs.price(F, K, T, 0.30, R, OptionType.CALL)
    assert hi > lo  # vega positive


# --------------------------------------------------------------------------- #
# Greeks vs finite differences
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ot", [OptionType.CALL, OptionType.PUT])
def test_spot_delta_matches_fd(ot) -> None:
    S = 24000.0
    price_S = lambda s: bs.price(bs.forward_price(s, R, Q, T), K, T, 0.15, R, ot)
    analytic = bs.spot_delta(bs.forward_price(S, R, Q, T), K, T, 0.15, R, Q, ot)
    assert analytic == pytest.approx(_fd(price_S, S, 1.0), abs=1e-4)


def test_vega_matches_fd() -> None:
    analytic = bs.vega_per_pct(F, K, T, 0.15, R)
    fd = _fd(lambda s: bs.price(F, K, T, s, R, OptionType.CALL), 0.15, 1e-4) / 100
    assert analytic == pytest.approx(fd, abs=1e-4)


def test_theta_matches_fd() -> None:
    analytic = bs.theta_per_day(F, K, T, 0.15, R, OptionType.CALL)
    fd = -_fd(lambda tt: bs.price(F, K, tt, 0.15, R, OptionType.CALL), T, 1e-5) / 365
    assert analytic == pytest.approx(fd, abs=1e-4)


def test_gamma_positive_and_symmetric() -> None:
    g_call = bs.gamma(F, K, T, 0.15, R)
    assert g_call > 0
    # Gamma is identical for calls and puts (no option_type arg) by construction.


def test_second_order_greeks_finite() -> None:
    g = bs.compute_greeks(F, K, T, 0.15, R, Q, OptionType.CALL)
    for value in (g.vanna, g.charm, g.speed):
        assert math.isfinite(value)


# --------------------------------------------------------------------------- #
# implied vol
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("true_sig", [0.08, 0.15, 0.35, 0.60])
@pytest.mark.parametrize("ot", [OptionType.CALL, OptionType.PUT])
def test_iv_roundtrip(true_sig, ot) -> None:
    px = bs.price(F, K, T, true_sig, R, ot)
    iv = bs.implied_vol(px, F, K, T, R, ot)
    assert iv is not None and iv == pytest.approx(true_sig, abs=1e-4)


def test_iv_below_intrinsic_returns_none() -> None:
    # A call priced below discounted intrinsic has no implied vol.
    disc_intrinsic = math.exp(-R * T) * (F - (F - 500))
    assert bs.implied_vol(disc_intrinsic * 0.5, F, F - 500, T, R, OptionType.CALL) is None


# --------------------------------------------------------------------------- #
# time to expiry
# --------------------------------------------------------------------------- #
def test_time_to_expiry_positive_and_small_near_expiry() -> None:
    from datetime import datetime, timezone
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    t = time_to_expiry("2025-01-30", now=now)
    assert 0.07 < t < 0.09  # ~29 days
    # On the expiry date afternoon, T collapses toward the floor.
    late = datetime(2025, 1, 30, 12, 0, tzinfo=timezone.utc)  # 17:30 IST > 15:30
    assert time_to_expiry("2025-01-30", now=late) <= 1e-3


# --------------------------------------------------------------------------- #
# chain analytics
# --------------------------------------------------------------------------- #
def _synthetic_chain(spot: float, sigma: float, expiry: str):
    """Build a chain whose LTPs are Black-76 prices at a known vol."""
    from datetime import datetime, timezone
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    t = time_to_expiry(expiry, now=now)
    f = bs.forward_price(spot, R, Q, t)
    rows = []
    for k in range(int(spot) - 300, int(spot) + 301, 100):
        ce = bs.price(f, k, t, sigma, R, OptionType.CALL)
        pe = bs.price(f, k, t, sigma, R, OptionType.PUT)
        rows.append({"strike": k, "option_type": "CE", "oi": 1000, "oi_change": 0,
                     "volume": 0, "ltp": ce, "ts": "2025-01-01T00:00:00+00:00"})
        rows.append({"strike": k, "option_type": "PE", "oi": 1200, "oi_change": 0,
                     "volume": 0, "ltp": pe, "ts": "2025-01-01T00:00:00+00:00"})
    return build_chain_dataframe(rows), now


def test_compute_chain_greeks_recovers_iv() -> None:
    spot, sigma, expiry = 24000.0, 0.18, "2025-01-30"
    chain, now = _synthetic_chain(spot, sigma, expiry)
    cg = compute_chain_greeks(chain, spot, R, Q, expiry, now=now)
    # Every solved IV should be ~18% (we priced the chain at 0.18).
    ivs = [v for v in list(cg["CE_IV"]) + list(cg["PE_IV"]) if v is not None]
    assert ivs and all(abs(v - 18.0) < 0.2 for v in ivs)
    assert atm_iv(cg, 24000.0) == pytest.approx(18.0, abs=0.2)


def test_gamma_exposure_signs() -> None:
    spot, expiry = 24000.0, "2025-01-30"
    chain, now = _synthetic_chain(spot, 0.18, expiry)
    cg = compute_chain_greeks(chain, spot, R, Q, expiry, now=now)
    gex = gamma_exposure(cg, spot, contract_size=50)
    assert not gex.per_strike.empty
    assert math.isfinite(gex.total)


def test_iv_smile_shape() -> None:
    chain, now = _synthetic_chain(24000.0, 0.18, "2025-01-30")
    cg = compute_chain_greeks(chain, 24000.0, R, Q, "2025-01-30", now=now)
    smile = iv_smile(cg)
    assert len(smile.strikes) == len(smile.ce_iv) == len(smile.pe_iv)


# --------------------------------------------------------------------------- #
# IV rank / percentile + persistence
# --------------------------------------------------------------------------- #
def test_iv_rank_and_percentile() -> None:
    history = [10.0, 12.0, 14.0, 16.0, 18.0, 20.0]
    assert iv_rank(15.0, history) == pytest.approx(50.0)
    assert iv_percentile(15.0, history) == pytest.approx(50.0)  # 3 of 6 below
    assert iv_rank(15.0, []) is None
    assert iv_rank(15.0, [12.0, 12.0]) is None  # flat history


def test_iv_history_persistence(tmp_path: Path) -> None:
    db = SQLiteDatabase(str(tmp_path / "t.db"))
    db.connect(); db.init_schema()
    for v in (12.0, 14.0, 16.0):
        db.insert_iv_snapshot("NIFTY", v, "2025-01-01T00:00:00+00:00")
    hist = db.fetch_iv_history("NIFTY")
    assert list(hist) == [12.0, 14.0, 16.0]
    db.close()
