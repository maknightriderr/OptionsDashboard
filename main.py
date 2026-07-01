"""
Phase 1 entrypoint: log in, resolve NIFTY option strikes, and stream them to DB.

Run (after filling in .env)::

    python main.py

This deliberately runs as a standalone long-lived process. It is NOT meant to
run inside Streamlit — Streamlit reruns its script on every interaction and
cannot hold a 24/7 websocket. The dashboard (Phase 2) will be a separate
process that only *reads* from the database this collector writes.
"""

from __future__ import annotations

import logging
import signal
import sys
from types import FrameType

from api.session import AngelOneSession
from collectors.instruments import (
    InstrumentRepository,
    download_scrip_master,
)
from collectors.market_data import MarketDataCollector
from config.settings import get_settings
from database.factory import make_database
from utils.logging import configure_logging

logger = logging.getLogger(__name__)


def _fetch_spot_ltp(session: AngelOneSession, exchange: str, symbol: str, token: str) -> float:
    """Fetch the underlying spot LTP via REST to seed ATM strike selection."""
    response = session.client.ltpData(exchange, symbol, token)  # type: ignore[attr-defined]
    if not response or not response.get("status"):
        raise RuntimeError(f"ltpData failed for {symbol}: {response}")
    return float(response["data"]["ltp"])


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir)
    logger.info("Starting option terminal (Phase 1) for indices: %s", settings.indices)

    # 1. Database -------------------------------------------------------------
    database = make_database(settings)
    database.connect()
    database.init_schema()

    # 2. Login ----------------------------------------------------------------
    session = AngelOneSession(settings)
    session.login()

    # 3. Instruments ----------------------------------------------------------
    master_path = download_scrip_master(
        settings.scrip_master_url,
        settings.scrip_master_cache,
        settings.scrip_master_max_age_hours,
    )
    repo = InstrumentRepository.from_file(master_path)

    # Phase 1 streams the first configured index only (NIFTY by default).
    index_name = settings.indices[0]
    spot_token = repo.resolve_spot_token(index_name)
    spot = _fetch_spot_ltp(session, "NSE", index_name, spot_token)
    logger.info("%s spot = %.2f", index_name, spot)

    instruments = repo.select_option_instruments(
        index_name, spot, settings.strikes_around_atm
    )

    # 4. Collector ------------------------------------------------------------
    collector = MarketDataCollector(
        session=session,
        database=database,
        settings=settings,
        index_name=index_name,
        spot_token=spot_token,
        instruments=instruments,
    )

    def _shutdown(_sig: int, _frame: FrameType | None) -> None:
        logger.info("Shutdown signal received; stopping collector.")
        collector.stop()
        database.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    collector.start()  # blocks until the socket closes
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
