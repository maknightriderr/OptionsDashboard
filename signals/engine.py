"""
The signal engine.

Pipeline per evaluation:
  1. Compute indicators from the current chain (+ an earlier snapshot for flow).
  2. Each indicator casts a *vote*: a direction (+1 bullish / -1 bearish), a
     strength in [0,1], a weight, and a one-line reason.
  3. Votes are aggregated into a net directional score; confidence/probability/
     risk are derived from the magnitude and the *agreement* among votes.
  4. If confidence clears a threshold, a trade frame is built using R-multiple
     targets anchored on the OI-wall stop, and a Signal is emitted.

This design keeps each indicator independent and testable, and makes adding new
rules a matter of writing another vote function (open/closed principle).

DISCLAIMER: heuristic market-structure logic, not trading advice. Scores express
indicator agreement, not calibrated probabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from analytics.indicators import (
    SupportResistance,
    compute_max_pain,
    compute_pcr,
    find_atm_strike,
    oi_change_breakdown,
    support_resistance,
)
from signals.models import Direction, Signal, SignalKind


@dataclass(frozen=True)
class SignalConfig:
    """Tunable thresholds (injected, never hardcoded at call sites)."""

    pcr_bullish: float = 1.3          # PCR above this leans bullish
    pcr_bearish: float = 0.7          # PCR below this leans bearish
    near_level_pct: float = 0.004     # "near" a S/R wall = within 0.4% of spot
    min_confidence: int = 45          # below this, no signal is emitted
    default_sl_pct: float = 0.004     # fallback stop distance if no wall is usable
    # Indicator weights (relative importance in the aggregate).
    w_pcr: float = 1.0
    w_max_pain: float = 0.8
    w_levels: float = 1.2
    w_oi_flow: float = 1.0


@dataclass
class _Vote:
    direction: int           # +1 bullish, -1 bearish, 0 neutral
    strength: float          # 0..1
    weight: float
    reason: str
    label: str               # short indicator label for supporting_indicators


@dataclass
class _Evaluation:
    pcr: float | None
    max_pain: float | None
    atm: float | None
    levels: SupportResistance
    votes: list[_Vote] = field(default_factory=list)


class SignalEngine:
    """Stateless evaluator: feed it snapshots, get signals back."""

    def __init__(self, config: SignalConfig | None = None) -> None:
        self._cfg = config or SignalConfig()

    # ---- public API ---------------------------------------------------------
    def evaluate(
        self,
        index_name: str,
        spot: float | None,
        current: pd.DataFrame,
        previous: pd.DataFrame | None = None,
    ) -> list[Signal]:
        """Return zero or one primary signal for the snapshot (list for future growth)."""
        if spot is None or current.empty:
            return []

        ev = self._gather(spot, current, previous)
        net, total_weight, agreement = self._aggregate(ev.votes)
        if total_weight == 0:
            return []

        confidence = int(round(min(abs(net), 1.0) * 100))
        if confidence < self._cfg.min_confidence:
            return []

        direction = Direction.BULLISH if net > 0 else Direction.BEARISH
        probability = self._probability(net, agreement)
        risk = self._risk(confidence, agreement)
        kind = self._classify_kind(direction, spot, ev)
        entry, sl, t1, t2, t3 = self._trade_frame(direction, spot, ev.levels)

        supporting = [v.label for v in ev.votes if v.direction == (1 if net > 0 else -1)]
        reason = self._compose_reason(direction, ev, net)

        return [
            Signal(
                index_name=index_name,
                direction=direction,
                kind=kind,
                spot=spot,
                confidence=confidence,
                risk=risk,
                probability=probability,
                entry=round(entry, 2),
                stop_loss=round(sl, 2),
                target1=round(t1, 2),
                target2=round(t2, 2),
                target3=round(t3, 2),
                reason=reason,
                supporting_indicators=supporting,
            )
        ]

    # ---- indicator gathering / voting --------------------------------------
    def _gather(
        self, spot: float, current: pd.DataFrame, previous: pd.DataFrame | None
    ) -> _Evaluation:
        cfg = self._cfg
        pcr = compute_pcr(current)
        max_pain = compute_max_pain(current)
        atm = find_atm_strike(spot, current)
        levels = support_resistance(current)
        ev = _Evaluation(pcr=pcr, max_pain=max_pain, atm=atm, levels=levels)

        # Vote 1 — PCR.
        if pcr is not None:
            if pcr >= cfg.pcr_bullish:
                strength = min((pcr - cfg.pcr_bullish) / cfg.pcr_bullish + 0.4, 1.0)
                ev.votes.append(_Vote(1, strength, cfg.w_pcr, f"PCR {pcr} is elevated", "PCR"))
            elif pcr <= cfg.pcr_bearish:
                strength = min((cfg.pcr_bearish - pcr) / cfg.pcr_bearish + 0.4, 1.0)
                ev.votes.append(_Vote(-1, strength, cfg.w_pcr, f"PCR {pcr} is depressed", "PCR"))

        # Vote 2 — Max pain pull (price tends toward max pain into expiry).
        if max_pain is not None:
            diff = (max_pain - spot) / spot
            if abs(diff) >= 0.001:
                direction = 1 if diff > 0 else -1
                strength = min(abs(diff) / 0.01, 1.0)  # full strength ~1% away
                ev.votes.append(
                    _Vote(direction, strength, cfg.w_max_pain,
                          f"Spot {'below' if diff > 0 else 'above'} max pain {max_pain:.0f}",
                          "MaxPain")
                )

        # Vote 3 — proximity to OI walls. A put-OI wall only acts as support when
        # it sits at/below spot; a call-OI wall only as resistance at/above spot.
        # The side check also avoids a degenerate double-vote when flat OI makes
        # support and resistance resolve to the same strike.
        if levels.support is not None and levels.support <= spot \
                and (spot - levels.support) / spot <= cfg.near_level_pct:
            ev.votes.append(_Vote(1, 0.7, cfg.w_levels,
                                  f"Spot near put-OI support {levels.support:.0f}", "Support"))
        if levels.resistance is not None and levels.resistance >= spot \
                and (levels.resistance - spot) / spot <= cfg.near_level_pct:
            ev.votes.append(_Vote(-1, 0.7, cfg.w_levels,
                                  f"Spot near call-OI resistance {levels.resistance:.0f}", "Resistance"))

        # Vote 4 — OI flow bias between snapshots.
        flow = oi_change_breakdown(current, previous)
        if abs(flow.bias) >= 0.2:
            direction = 1 if flow.bias > 0 else -1
            ev.votes.append(
                _Vote(direction, min(abs(flow.bias), 1.0), cfg.w_oi_flow,
                      f"OI flow bias {flow.bias:+.2f} "
                      f"(ΔPE {flow.pe_oi_change:+d}, ΔCE {flow.ce_oi_change:+d})", "OIFlow")
            )
        return ev

    # ---- scoring ------------------------------------------------------------
    @staticmethod
    def _aggregate(votes: list[_Vote]) -> tuple[float, float, float]:
        """Return (net score in [-1,1], total weight, agreement fraction 0..1)."""
        total_weight = sum(v.weight for v in votes)
        if total_weight == 0:
            return 0.0, 0.0, 0.0
        weighted = sum(v.direction * v.strength * v.weight for v in votes)
        net = weighted / total_weight
        # Agreement: weight fraction voting with the net sign.
        sign = 1 if net > 0 else -1 if net < 0 else 0
        agree_w = sum(v.weight for v in votes if v.direction == sign and sign != 0)
        agreement = agree_w / total_weight if total_weight else 0.0
        return net, total_weight, agreement

    @staticmethod
    def _probability(net: float, agreement: float) -> int:
        """Heuristic lean: starts at 50, lifted by magnitude × agreement."""
        return int(round(min(50 + abs(net) * 40 * agreement, 95)))

    @staticmethod
    def _risk(confidence: int, agreement: float) -> int:
        """Higher when indicators disagree or conviction is thin."""
        return int(round(max(0, min(100, 100 - confidence * agreement))))

    def _classify_kind(self, direction: Direction, spot: float, ev: _Evaluation) -> SignalKind:
        levels = ev.levels
        near = self._cfg.near_level_pct
        if direction is Direction.BULLISH and levels.support is not None \
                and levels.support <= spot and (spot - levels.support) / spot <= near:
            return SignalKind.REVERSAL          # bounce off support
        if direction is Direction.BEARISH and levels.resistance is not None \
                and levels.resistance >= spot and (levels.resistance - spot) / spot <= near:
            return SignalKind.BREAKDOWN         # rejection at resistance
        # If PCR + OI flow both present and agree, call it trend-following.
        labels = {v.label for v in ev.votes}
        if {"PCR", "OIFlow"}.issubset(labels):
            return SignalKind.TREND_FOLLOWING
        return SignalKind.MOMENTUM

    # ---- trade frame --------------------------------------------------------
    def _trade_frame(
        self, direction: Direction, spot: float, levels: SupportResistance
    ) -> tuple[float, float, float, float, float]:
        """
        Build entry/SL/T1-3 using R-multiple targets anchored on the OI wall.

        Risk unit R = |entry - stop|. Targets are 1R/2R/3R from entry. The stop
        sits just beyond the relevant wall when one exists on the correct side,
        else falls back to a fixed percentage of spot.
        """
        cfg = self._cfg
        entry = spot
        if direction is Direction.BULLISH:
            if levels.support is not None and levels.support < spot:
                stop = levels.support * (1 - 0.001)
            else:
                stop = spot * (1 - cfg.default_sl_pct)
            r = max(entry - stop, spot * cfg.default_sl_pct)
            return entry, stop, entry + r, entry + 2 * r, entry + 3 * r
        # bearish
        if levels.resistance is not None and levels.resistance > spot:
            stop = levels.resistance * (1 + 0.001)
        else:
            stop = spot * (1 + cfg.default_sl_pct)
        r = max(stop - entry, spot * cfg.default_sl_pct)
        return entry, stop, entry - r, entry - 2 * r, entry - 3 * r

    @staticmethod
    def _compose_reason(direction: Direction, ev: _Evaluation, net: float) -> str:
        bits = [v.reason for v in ev.votes if v.direction == (1 if net > 0 else -1)]
        head = f"{direction.value.capitalize()} bias"
        if ev.pcr is not None:
            head += f" (PCR {ev.pcr}"
            if ev.max_pain is not None:
                head += f", max pain {ev.max_pain:.0f}"
            head += ")"
        return head + ": " + "; ".join(bits) if bits else head
