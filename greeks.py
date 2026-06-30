"""
Black-76 option analytics for index options.

Angel One does not provide Greeks, so we compute them. Index options are
European, so Black-76 (options on a forward) is the right model. The caller
supplies the forward price F; for an index you can derive it from spot via
``forward_price(spot, r, q, T)`` or pass a traded futures price directly.

Conventions and units (so the numbers are interpretable):
  * sigma is annualised volatility as a decimal (0.15 = 15%).
  * T is time to expiry in years.
  * delta is reported as SPOT delta (what an option chain shows): for a call it
    tends to 0..1, for a put -1..0. Internally we work in forward space and
    convert (spot_delta = forward_delta * e^{(r-q)T}).
  * vega is per 1 percentage-point change in vol (i.e. per 0.01).
  * theta and charm are per CALENDAR DAY (annual / 365).
  * rho is per 1 percentage-point change in rate.

First-order Greeks use closed forms; the second-order Greeks (vanna, charm,
speed) are computed by central finite differences off those validated
first-order functions. This trades a few extra evaluations for immunity to the
transcription errors that the exotic closed forms are notorious for — and the
tests cross-check the analytic first-order Greeks against finite differences of
the price, so the whole stack is self-consistent.

Nothing here is trading advice.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from database.models import OptionType

_SQRT_2PI = math.sqrt(2.0 * math.pi)
_MIN_T = 1.0 / (365.0 * 24.0 * 60.0)   # ~1 minute, to avoid div-by-zero at expiry
_MIN_SIGMA = 1e-6


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function (no scipy needed)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def forward_price(spot: float, r: float, q: float, t: float) -> float:
    """Forward/futures price of the index: F = S * e^{(r - q)T}."""
    return spot * math.exp((r - q) * t)


def d1_d2(f: float, k: float, t: float, sigma: float) -> tuple[float, float]:
    """Black-76 d1, d2."""
    t = max(t, _MIN_T)
    sigma = max(sigma, _MIN_SIGMA)
    vol_sqrt_t = sigma * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * sigma * sigma * t) / vol_sqrt_t
    return d1, d1 - vol_sqrt_t


def price(f: float, k: float, t: float, sigma: float, r: float, option_type: OptionType) -> float:
    """Black-76 option price (discounted at r)."""
    t = max(t, _MIN_T)
    disc = math.exp(-r * t)
    d1, d2 = d1_d2(f, k, t, sigma)
    if option_type is OptionType.CALL:
        return disc * (f * _norm_cdf(d1) - k * _norm_cdf(d2))
    return disc * (k * _norm_cdf(-d2) - f * _norm_cdf(-d1))


# --------------------------------------------------------------------------- #
# First-order Greeks (analytic)
# --------------------------------------------------------------------------- #
def _forward_delta(f: float, k: float, t: float, sigma: float, r: float, ot: OptionType) -> float:
    disc = math.exp(-r * t)
    d1, _ = d1_d2(f, k, t, sigma)
    if ot is OptionType.CALL:
        return disc * _norm_cdf(d1)
    return -disc * _norm_cdf(-d1)


def spot_delta(f: float, k: float, t: float, sigma: float, r: float, q: float, ot: OptionType) -> float:
    """Delta with respect to SPOT (chain-style)."""
    t = max(t, _MIN_T)
    return _forward_delta(f, k, t, sigma, r, ot) * math.exp((r - q) * t)


def gamma(f: float, k: float, t: float, sigma: float, r: float) -> float:
    """Gamma w.r.t. forward (same for calls and puts)."""
    t = max(t, _MIN_T)
    sigma = max(sigma, _MIN_SIGMA)
    d1, _ = d1_d2(f, k, t, sigma)
    return math.exp(-r * t) * _norm_pdf(d1) / (f * sigma * math.sqrt(t))


def vega_per_pct(f: float, k: float, t: float, sigma: float, r: float) -> float:
    """Vega per 1 percentage-point of vol (i.e. dPrice/dsigma / 100)."""
    t = max(t, _MIN_T)
    d1, _ = d1_d2(f, k, t, sigma)
    vega_unit = math.exp(-r * t) * f * _norm_pdf(d1) * math.sqrt(t)
    return vega_unit / 100.0


def theta_per_day(f: float, k: float, t: float, sigma: float, r: float, ot: OptionType) -> float:
    """Theta per calendar day."""
    t = max(t, _MIN_T)
    sigma = max(sigma, _MIN_SIGMA)
    disc = math.exp(-r * t)
    d1, d2 = d1_d2(f, k, t, sigma)
    term1 = -disc * f * _norm_pdf(d1) * sigma / (2.0 * math.sqrt(t))
    if ot is OptionType.CALL:
        annual = term1 + r * disc * f * _norm_cdf(d1) - r * disc * k * _norm_cdf(d2)
    else:
        annual = term1 - r * disc * f * _norm_cdf(-d1) + r * disc * k * _norm_cdf(-d2)
    return annual / 365.0


def rho_per_pct(f: float, k: float, t: float, sigma: float, r: float, ot: OptionType) -> float:
    """Rho per 1 percentage-point of rate. For Black-76, rho = -T * price."""
    t = max(t, _MIN_T)
    return -t * price(f, k, t, sigma, r, ot) / 100.0


# --------------------------------------------------------------------------- #
# Second-order Greeks (central finite differences off the analytic first-order)
# --------------------------------------------------------------------------- #
def vanna(f: float, k: float, t: float, sigma: float, r: float, q: float, ot: OptionType) -> float:
    """d(spot delta)/d(sigma), per 1 vol point. Same sign for calls/puts."""
    h = 1e-4
    up = spot_delta(f, k, t, sigma + h, r, q, ot)
    dn = spot_delta(f, k, t, sigma - h, r, q, ot)
    return (up - dn) / (2.0 * h) / 100.0


def charm(f: float, k: float, t: float, sigma: float, r: float, q: float, ot: OptionType) -> float:
    """Delta decay per calendar day: charm = -d(delta)/dT / 365."""
    h = min(1e-4, t / 2.0)
    up = spot_delta(f, k, t + h, sigma, r, q, ot)
    dn = spot_delta(f, k, t - h, sigma, r, q, ot)
    d_delta_dT = (up - dn) / (2.0 * h)
    return -d_delta_dT / 365.0


def speed(f: float, k: float, t: float, sigma: float, r: float) -> float:
    """d(gamma)/d(forward)."""
    h = f * 1e-4
    return (gamma(f + h, k, t, sigma, r) - gamma(f - h, k, t, sigma, r)) / (2.0 * h)


# --------------------------------------------------------------------------- #
# Bundled Greeks
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float      # per day
    vega: float       # per 1% vol
    rho: float        # per 1% rate
    vanna: float
    charm: float      # per day
    speed: float
    iv: float | None  # the sigma used (solved or supplied)


def compute_greeks(
    f: float, k: float, t: float, sigma: float, r: float, q: float, ot: OptionType
) -> Greeks:
    """Compute the full Greek set for one contract at a given vol."""
    t = max(t, _MIN_T)
    return Greeks(
        price=price(f, k, t, sigma, r, ot),
        delta=spot_delta(f, k, t, sigma, r, q, ot),
        gamma=gamma(f, k, t, sigma, r),
        theta=theta_per_day(f, k, t, sigma, r, ot),
        vega=vega_per_pct(f, k, t, sigma, r),
        rho=rho_per_pct(f, k, t, sigma, r, ot),
        vanna=vanna(f, k, t, sigma, r, q, ot),
        charm=charm(f, k, t, sigma, r, q, ot),
        speed=speed(f, k, t, sigma, r),
        iv=sigma,
    )


# --------------------------------------------------------------------------- #
# Implied volatility
# --------------------------------------------------------------------------- #
def implied_vol(
    market_price: float, f: float, k: float, t: float, r: float, ot: OptionType,
    lo: float = 1e-4, hi: float = 10.0, tol: float = 1e-7, max_iter: int = 100,
) -> float | None:
    """
    Solve Black-76 implied volatility from a market price.

    Hybrid Newton-Raphson with a bisection fallback so it is both fast and
    robust. Returns None when the price is below intrinsic value or otherwise
    has no admissible solution.
    """
    t = max(t, _MIN_T)
    if market_price <= 0.0:
        return None

    disc = math.exp(-r * t)
    intrinsic = disc * (max(f - k, 0.0) if ot is OptionType.CALL else max(k - f, 0.0))
    upper_bound = disc * (f if ot is OptionType.CALL else k)
    # Price must sit strictly between discounted intrinsic and the asymptotic cap.
    if market_price < intrinsic - tol or market_price > upper_bound + tol:
        return None

    def diff(sigma: float) -> float:
        return price(f, k, t, sigma, r, ot) - market_price

    # Bracket must straddle a root.
    f_lo, f_hi = diff(lo), diff(hi)
    if f_lo * f_hi > 0:
        return None

    # Newton from a sensible seed (Brenner-Subrahmanyam ATM approximation).
    sigma = max(min(math.sqrt(2.0 * math.pi / t) * market_price / (disc * f), hi), lo)
    for _ in range(max_iter):
        fx = diff(sigma)
        if abs(fx) < tol:
            return sigma
        v = vega_per_pct(f, k, t, sigma, r) * 100.0  # back to per-unit-vol
        if v < 1e-10:
            break  # vega too small; fall back to bisection
        step = fx / v
        sigma_next = sigma - step
        if sigma_next <= lo or sigma_next >= hi or math.isnan(sigma_next):
            break
        sigma = sigma_next

    # Bisection fallback (guaranteed to converge on the bracket).
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = diff(mid)
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)
