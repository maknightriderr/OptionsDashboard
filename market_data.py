"""
Live market-data collector (SmartWebSocketV2).

Phase 1 scope: connect to the feed, subscribe to one index spot plus its
selected option strikes, normalise each incoming packet, and persist it.

Design notes
------------
* The raw SmartWebSocketV2 callback delivers an already-decoded dict. Prices on
  NSE/NFO come in *paise* and must be divided by 100. Open interest and volume
  are absolute integers. We isolate that decoding in the pure functions
  ``normalise_spot_tick`` / ``normalise_option_tick`` so they can be unit-tested
  without a socket.
* Writes are *batched*. Committing per tick would hammer SQLite; instead option
  ticks accumulate in a buffer that flushes on size or on a timer thread.
* Subscription mode 3 (SNAP_QUOTE) is required because it is the mode that
  carries open interest and the best-5 depth we need for bid/ask.

Exchange type codes (SmartWebSocketV2): 1 = NSE cash, 2 = NSE F&O.
Subscription modes: 1 = LTP, 2 = QUOTE, 3 = SNAP_QUOTE.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from config.settings import Settings
from database.interface import Database
from database.models import Instrument, OptionTick, OptionType, SpotTick

logger = logging.getLogger(__name__)

# SmartWebSocketV2 constants
EXCH_NSE_CASH = 1
EXCH_NSE_FO = 2
MODE_SNAP_QUOTE = 3

_PAISE = 100.0


def _to_rupees(paise: Any) -> float:
    """Convert a paise integer (possibly None) to rupees float."""
    try:
        return float(paise) / _PAISE
    except (TypeError, ValueError):
        return 0.0


def normalise_spot_tick(raw: dict[str, Any], name: str) -> SpotTick:
    """Convert a raw spot packet into a SpotTick (pure / testable)."""
    return SpotTick(
        name=name,
        token=str(raw.get("token", "")),
        ltp=_to_rupees(raw.get("last_traded_price")),
        ts=datetime.now(tz=timezone.utc),
    )


def normalise_option_tick(raw: dict[str, Any], instrument: Instrument) -> OptionTick:
    """
    Convert a raw SNAP_QUOTE packet into an OptionTick (pure / testable).

    Best bid/ask are taken from the top of the best-5 depth arrays when present.
    """
    best_buy = raw.get("best_5_buy_data") or []
    best_sell = raw.get("best_5_sell_data") or []
    bid = _to_rupees(best_buy[0]["price"]) if best_buy else 0.0
    ask = _to_rupees(best_sell[0]["price"]) if best_sell else 0.0

    return OptionTick(
        token=instrument.token,
        name=instrument.name,
        strike=instrument.strike,
        option_type=instrument.option_type,
        expiry=instrument.expiry,
        ltp=_to_rupees(raw.get("last_traded_price")),
        volume=int(raw.get("volume_trade_for_the_day", 0) or 0),
        oi=int(raw.get("open_interest", 0) or 0),
        oi_change=int(raw.get("open_interest_change", 0) or 0),
        bid=bid,
        ask=ask,
        ts=datetime.now(tz=timezone.utc),
    )


class MarketDataCollector:
    """Owns the websocket subscription for one index and persists its ticks."""

    def __init__(
        self,
        session: Any,                 # AngelOneSession (typed loosely to avoid cycle)
        database: Database,
        settings: Settings,
        index_name: str,
        spot_token: str,
        instruments: Sequence[Instrument],
        flush_size: int = 50,
        flush_interval_sec: float = 2.0,
    ) -> None:
        self._session = session
        self._db = database
        self._settings = settings
        self._index_name = index_name
        self._spot_token = spot_token
        # token -> Instrument, for O(1) routing of incoming packets.
        self._by_token: dict[str, Instrument] = {i.token: i for i in instruments}

        self._buffer: list[OptionTick] = []
        self._buffer_lock = threading.Lock()
        self._flush_size = flush_size
        self._flush_interval = flush_interval_sec
        self._stop = threading.Event()
        self._ws: Any | None = None

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Open the websocket and register callbacks (blocks in connect())."""
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2  # lazy import

        tokens = self._session.tokens
        self._ws = SmartWebSocketV2(
            tokens.jwt_token,
            self._settings.api_key
            if not self._settings.credentials_encrypted
            else self._decrypted_api_key(),
            self._settings.client_code
            if not self._settings.credentials_encrypted
            else self._decrypted_client_code(),
            tokens.feed_token,
        )
        self._ws.on_open = self._on_open
        self._ws.on_data = self._on_data
        self._ws.on_error = self._on_error
        self._ws.on_close = self._on_close

        # Periodic flusher so low-traffic strikes still get persisted promptly.
        threading.Thread(target=self._flush_loop, name="flush-loop", daemon=True).start()

        logger.info("Connecting websocket for %s ...", self._index_name)
        self._ws.connect()  # blocking; SDK handles its own reconnect loop

    def stop(self) -> None:
        self._stop.set()
        self._flush()  # drain whatever is buffered
        if self._ws is not None:
            try:
                self._ws.close_connection()
            except Exception:  # noqa: BLE001
                logger.debug("Error closing websocket (ignored).", exc_info=True)

    # ---- websocket callbacks ------------------------------------------------
    def _on_open(self, _wsapp: Any) -> None:
        """Subscribe to spot + option tokens once the socket is open."""
        token_list = [
            {"exchangeType": EXCH_NSE_CASH, "tokens": [self._spot_token]},
            {"exchangeType": EXCH_NSE_FO, "tokens": list(self._by_token.keys())},
        ]
        correlation_id = f"ot-{self._index_name.lower()}"
        self._ws.subscribe(correlation_id, MODE_SNAP_QUOTE, token_list)  # type: ignore[union-attr]
        logger.info(
            "Subscribed: 1 spot + %d option tokens for %s.",
            len(self._by_token), self._index_name,
        )

    def _on_data(self, _wsapp: Any, message: dict[str, Any]) -> None:
        """Route, normalise, and buffer/persist a single packet."""
        token = str(message.get("token", ""))
        try:
            if token == self._spot_token:
                self._db.insert_spot(normalise_spot_tick(message, self._index_name))
                return
            instrument = self._by_token.get(token)
            if instrument is None:
                return  # unsubscribed / unexpected token
            tick = normalise_option_tick(message, instrument)
            self._buffer_tick(tick)
        except Exception:  # noqa: BLE001 - never let one bad packet kill the feed
            logger.exception("Failed to process packet for token %s.", token)

    def _on_error(self, _wsapp: Any, error: Any) -> None:
        logger.error("Websocket error: %s", error)

    def _on_close(self, _wsapp: Any, *_args: Any) -> None:
        logger.warning("Websocket closed for %s.", self._index_name)

    # ---- buffering ----------------------------------------------------------
    def _buffer_tick(self, tick: OptionTick) -> None:
        with self._buffer_lock:
            self._buffer.append(tick)
            full = len(self._buffer) >= self._flush_size
        if full:
            self._flush()

    def _flush(self) -> None:
        with self._buffer_lock:
            if not self._buffer:
                return
            batch, self._buffer = self._buffer, []
        written = self._db.insert_option_ticks(batch)
        logger.debug("Flushed %d option ticks.", written)

    def _flush_loop(self) -> None:
        while not self._stop.wait(self._flush_interval):
            self._flush()

    # ---- credential helpers (only used when encrypted) ----------------------
    def _decrypted_api_key(self) -> str:
        from utils import crypto
        return crypto.decrypt(self._settings.api_key, self._settings.require_encryption_key())

    def _decrypted_client_code(self) -> str:
        from utils import crypto
        return crypto.decrypt(self._settings.client_code, self._settings.require_encryption_key())
