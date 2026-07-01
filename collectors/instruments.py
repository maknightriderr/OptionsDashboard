"""
Instrument resolution from Angel One's scrip master.

The WebSocket subscribes by *token*, not by human symbol, so before we can
collect anything we must:

  1. download the scrip master (a large JSON of every tradable instrument),
  2. find the spot token for each index,
  3. pick the nearest expiry, and
  4. select ATM +/- N strikes (both CE and PE) and map them to tokens.

All of the parsing/selection logic is pure and unit-tested; only
``download_scrip_master`` touches the network.

Scrip master row shape (relevant fields)::

    {
      "token": "54321",
      "symbol": "NIFTY30JAN2524500CE",
      "name": "NIFTY",
      "expiry": "30JAN2025",            # DDMMMYYYY
      "strike": "2450000.000000",       # strike * 100
      "lotsize": "75",
      "instrumenttype": "OPTIDX",       # OPTIDX = index option
      "exch_seg": "NFO"
    }
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

from database.models import Instrument, OptionType

logger = logging.getLogger(__name__)

# Index spot reference tokens (NSE cash segment). These are stable exchange
# reference data, but should still be verified against the loaded master via
# InstrumentRepository.resolve_spot_token, which prefers a live lookup and only
# falls back to this map.
INDEX_SPOT_TOKENS: dict[str, str] = {
    "NIFTY": "26000",
    "BANKNIFTY": "26009",
    "FINNIFTY": "26037",
    "MIDCPNIFTY": "26074",
}

_OPTION_INSTRUMENT_TYPE = "OPTIDX"
_STRIKE_DIVISOR = 100.0  # master stores strike * 100


# --------------------------------------------------------------------------- #
# Download / load
# --------------------------------------------------------------------------- #
def download_scrip_master(
    url: str, cache_path: str, max_age_hours: int = 12, timeout: int = 30
) -> str:
    """
    Ensure a reasonably fresh scrip master exists at ``cache_path``; return it.

    Uses the cached copy if it is younger than ``max_age_hours`` so we are not
    pulling a multi-MB file on every restart. Network failures fall back to any
    existing cache (with a warning) so a transient outage does not stop startup.
    """
    cache = Path(cache_path)
    if cache.exists():
        age_hours = (time.time() - cache.stat().st_mtime) / 3600.0
        if age_hours < max_age_hours:
            logger.info("Using cached scrip master (age %.1fh).", age_hours)
            return str(cache)

    cache.parent.mkdir(parents=True, exist_ok=True)
    try:
        logger.info("Downloading scrip master from %s", url)
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        cache.write_bytes(response.content)
        logger.info("Scrip master saved (%d bytes).", len(response.content))
    except requests.RequestException as exc:
        if cache.exists():
            logger.warning("Download failed (%s); using stale cache.", exc)
        else:
            raise RuntimeError(f"Scrip master download failed and no cache: {exc}") from exc
    return str(cache)


def load_scrip_master(path: str) -> pd.DataFrame:
    """Load the scrip master JSON file into a DataFrame."""
    with open(path, "r", encoding="utf-8") as handle:
        records = json.load(handle)
    frame = pd.DataFrame(records)
    logger.info("Loaded scrip master: %d rows.", len(frame))
    return frame


def _parse_expiry(value: str) -> date:
    """Parse Angel's DDMMMYYYY expiry (e.g. '30JAN2025') into a date."""
    return datetime.strptime(value.strip().upper(), "%d%b%Y").date()


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class InstrumentRepository:
    """Queryable view over the scrip master for one process lifetime."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame

    @classmethod
    def from_file(cls, path: str) -> "InstrumentRepository":
        return cls(load_scrip_master(path))

    # ---- spot ---------------------------------------------------------------
    def resolve_spot_token(self, index_name: str) -> str:
        """
        Return the NSE spot token for an index.

        Prefers a live lookup in the master (exch_seg == NSE, matching name);
        falls back to the reference map if the master shape is unexpected.
        """
        name = index_name.upper()
        try:
            nse = self._frame[self._frame["exch_seg"] == "NSE"]
            # Index rows carry the index name in 'name' with empty instrumenttype.
            match = nse[nse["name"].str.upper() == name]
            if not match.empty:
                return str(match.iloc[0]["token"])
        except (KeyError, AttributeError):
            pass
        if name in INDEX_SPOT_TOKENS:
            logger.warning("Spot token for %s from fallback map.", name)
            return INDEX_SPOT_TOKENS[name]
        raise ValueError(f"Could not resolve spot token for index {index_name!r}.")

    # ---- options ------------------------------------------------------------
    def _options_for(self, index_name: str) -> pd.DataFrame:
        name = index_name.upper()
        frame = self._frame
        mask = (
            (frame["name"].str.upper() == name)
            & (frame["instrumenttype"] == _OPTION_INSTRUMENT_TYPE)
        )
        return frame[mask].copy()

    def nearest_expiry(self, index_name: str, on: date | None = None) -> date:
        """Return the soonest expiry on or after ``on`` (default: today)."""
        on = on or date.today()
        options = self._options_for(index_name)
        if options.empty:
            raise ValueError(f"No options found for {index_name!r} in scrip master.")
        expiries = sorted({_parse_expiry(e) for e in options["expiry"].unique()})
        future = [e for e in expiries if e >= on]
        if not future:
            raise ValueError(f"No current/future expiry for {index_name!r} (today={on}).")
        return future[0]

    @staticmethod
    def _infer_strike_step(strikes: list[float]) -> float:
        """Infer the strike interval from the sorted unique strike list."""
        unique = sorted(set(strikes))
        if len(unique) < 2:
            return 50.0  # sensible default for index options
        diffs = [round(b - a, 2) for a, b in zip(unique, unique[1:]) if b > a]
        return min(diffs) if diffs else 50.0

    def select_option_instruments(
        self,
        index_name: str,
        spot: float,
        strikes_around_atm: int,
        expiry: date | None = None,
    ) -> list[Instrument]:
        """
        Select ATM +/- N strikes (both CE and PE) for the chosen expiry.

        ``spot`` is the current underlying price; ATM is the nearest available
        strike to it. Returns up to (2N+1) * 2 Instrument objects.
        """
        options = self._options_for(index_name)
        target_expiry = expiry or self.nearest_expiry(index_name)
        options = options[
            options["expiry"].map(_parse_expiry) == target_expiry
        ].copy()
        if options.empty:
            raise ValueError(
                f"No {index_name} options for expiry {target_expiry.isoformat()}."
            )

        options["strike_val"] = options["strike"].astype(float) / _STRIKE_DIVISOR
        all_strikes = options["strike_val"].tolist()
        step = self._infer_strike_step(all_strikes)
        atm = round(spot / step) * step

        lo = atm - strikes_around_atm * step
        hi = atm + strikes_around_atm * step
        window = options[
            (options["strike_val"] >= lo) & (options["strike_val"] <= hi)
        ]

        instruments: list[Instrument] = []
        for _, row in window.iterrows():
            symbol = str(row["symbol"]).upper()
            opt_type = OptionType.CALL if symbol.endswith("CE") else OptionType.PUT
            instruments.append(
                Instrument(
                    token=str(row["token"]),
                    symbol=symbol,
                    name=index_name.upper(),
                    exchange=str(row.get("exch_seg", "NFO")),
                    strike=float(row["strike_val"]),
                    option_type=opt_type,
                    expiry=target_expiry.isoformat(),
                    lot_size=int(float(row["lotsize"])),
                )
            )
        instruments.sort(key=lambda i: (i.strike, i.option_type.value))
        logger.info(
            "Selected %d contracts for %s exp %s (ATM=%.0f, step=%.0f).",
            len(instruments), index_name, target_expiry, atm, step,
        )
        return instruments
