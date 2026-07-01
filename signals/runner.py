"""
Signal runner.

Glues the database (read), the engine (compute), and the database (write)
together. ``evaluate_and_store`` runs one cycle for one index; ``run_loop``
keeps doing it on an interval for all configured indices.

It can run either as its own process (``python -m signals.runner``) or be
invoked from the collector. It only needs read access to ticks and write access
to the signals table, so it uses a normal (read-write) connection.

A light cooldown prevents storing a near-identical signal on every cycle — full
alerting/priority/duplicate-prevention is Phase 4, this is just enough to keep
the table readable.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from analytics.chain_analytics import atm_iv, compute_chain_greeks
from analytics.indicators import build_chain_dataframe, find_atm_strike
from config.settings import Settings, get_settings
from database.factory import make_database
from database.interface import Database
from signals.engine import SignalConfig, SignalEngine
from signals.models import Signal
from utils.logging import configure_logging

logger = logging.getLogger(__name__)


def _is_duplicate(db: Database, signal: Signal, cooldown_sec: int) -> bool:
    """True if the last stored signal matches direction+kind within the cooldown."""
    recent = db.fetch_recent_signals(signal.index_name, limit=1)
    if not recent:
        return False
    last = recent[0]
    if last["direction"] != signal.direction.value or last["kind"] != signal.kind.value:
        return False
    try:
        last_ts = datetime.fromisoformat(last["ts"])
    except (ValueError, KeyError):
        return False
    age = (signal.ts - last_ts).total_seconds()
    return age < cooldown_sec


def evaluate_and_store(
    db: Database,
    engine: SignalEngine,
    index_name: str,
    lookback_sec: int = 300,
    cooldown_sec: int = 120,
    dispatcher: object | None = None,
    record_iv: bool = False,
    rfr: float = 0.065,
    div_yield: float = 0.012,
) -> list[Signal]:
    """Run one evaluation cycle for ``index_name``; persist non-duplicate signals.

    When a ``dispatcher`` (AlertDispatcher) is supplied, each newly stored signal
    is also pushed through the alert pipeline (priority gate + cooldown + channels).
    """
    rows = list(db.fetch_latest_option_chain(index_name))
    if not rows:
        logger.debug("[%s] no chain data yet.", index_name)
        return []

    spot_row = db.fetch_latest_spot(index_name)
    spot = float(spot_row["ltp"]) if spot_row else None

    cutoff = (datetime.now(tz=timezone.utc) - timedelta(seconds=lookback_sec)).isoformat()
    prev_rows = list(db.fetch_option_chain_asof(index_name, cutoff))

    current = build_chain_dataframe(rows)
    previous = build_chain_dataframe(prev_rows) if prev_rows else None

    # Record an ATM IV snapshot so IV rank/percentile can build over time.
    if record_iv and spot is not None:
        try:
            expiry = min(r.get("expiry", "") for r in rows if r.get("expiry"))
            atm = find_atm_strike(spot, current)
            cg = compute_chain_greeks(current, spot, rfr, div_yield, expiry)
            aiv = atm_iv(cg, atm)
            if aiv is not None:
                db.insert_iv_snapshot(
                    index_name, aiv, datetime.now(tz=timezone.utc).isoformat()
                )
        except Exception:  # noqa: BLE001 - IV recording must not break signals
            logger.exception("IV snapshot failed for %s.", index_name)

    signals = engine.evaluate(index_name, spot, current, previous)
    stored: list[Signal] = []
    for signal in signals:
        if _is_duplicate(db, signal, cooldown_sec):
            logger.debug("[%s] duplicate %s/%s within cooldown; skipping.",
                         index_name, signal.direction.value, signal.kind.value)
            continue
        db.insert_signal(signal)
        stored.append(signal)
        logger.info(
            "[%s] %s %s | conf=%d risk=%d prob=%d | entry=%.2f SL=%.2f T1=%.2f",
            index_name, signal.direction.value, signal.kind.value,
            signal.confidence, signal.risk, signal.probability,
            signal.entry, signal.stop_loss, signal.target1,
        )
        if dispatcher is not None:
            try:
                dispatcher.dispatch(signal)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - alerting must not break the loop
                logger.exception("Alert dispatch failed for %s.", index_name)
    return stored


def run_loop(settings: Settings | None = None, interval_sec: int = 30) -> None:
    """Continuously evaluate signals for all configured indices."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_dir)
    engine = SignalEngine(SignalConfig())

    db = make_database(settings)            # read ticks + write signals
    db.connect()
    db.init_schema()                        # ensures signals + alerts tables exist

    from alerts.factory import build_dispatcher
    dispatcher = build_dispatcher(settings, database=db)

    logger.info("Signal runner started for %s (every %ds).", settings.indices, interval_sec)

    try:
        while True:
            for index_name in settings.indices:
                try:
                    evaluate_and_store(
                        db, engine, index_name, dispatcher=dispatcher,
                        record_iv=True,
                        rfr=settings.risk_free_rate, div_yield=settings.dividend_yield,
                    )
                except Exception:  # noqa: BLE001 - one index must not kill the loop
                    logger.exception("Evaluation failed for %s.", index_name)
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        logger.info("Signal runner stopping.")
    finally:
        db.close()


if __name__ == "__main__":
    run_loop()
