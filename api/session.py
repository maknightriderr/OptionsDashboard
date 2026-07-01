"""
Angel One SmartAPI session manager.

Responsibilities:
  * decrypt credentials if they are stored encrypted,
  * log in via generateSession() using a time-based OTP,
  * hold the JWT / refresh / feed tokens,
  * refresh the session (Angel sessions die at midnight IST), and
  * re-login with exponential backoff on failure.

The ``SmartApi`` package is imported lazily inside methods so that the rest of
the project (DB, instruments, tests) imports cleanly on a machine that has not
installed the broker SDK. Install it with::

    pip install smartapi-python pyotp

References confirmed against the current SDK (smartapi-python 1.5.5):
  from SmartApi import SmartConnect
  data = smartApi.generateSession(client_code, pin, totp)
  feed = smartApi.getfeedToken()
  smartApi.generateToken(refresh_token)   # refresh
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import pyotp

from config.settings import Settings
from utils import crypto

logger = logging.getLogger(__name__)


@dataclass
class SessionTokens:
    """The three tokens the rest of the system needs after login."""

    jwt_token: str
    refresh_token: str
    feed_token: str
    issued_at: float = field(default_factory=time.time)


class AngelOneSession:
    """Owns the authenticated SmartConnect client and its tokens."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._client: object | None = None  # SmartConnect instance
        self._tokens: SessionTokens | None = None

    # ---- public API ---------------------------------------------------------
    @property
    def client(self) -> object:
        """The underlying SmartConnect client (must be logged in first)."""
        if self._client is None:
            raise RuntimeError("Not logged in. Call login() first.")
        return self._client

    @property
    def tokens(self) -> SessionTokens:
        if self._tokens is None:
            raise RuntimeError("Not logged in. Call login() first.")
        return self._tokens

    def login(self) -> SessionTokens:
        """Authenticate and populate tokens. Retries with exponential backoff."""
        with self._lock:
            self._login_with_backoff()
            assert self._tokens is not None
            return self._tokens

    def refresh(self) -> SessionTokens:
        """
        Refresh the access token using the stored refresh token.

        Angel sessions are valid until midnight IST; a long-running collector
        should call this proactively (see is_stale) rather than wait for a 401.
        """
        with self._lock:
            if self._client is None or self._tokens is None:
                return self._login_with_backoff()
            try:
                self._client.generateToken(self._tokens.refresh_token)  # type: ignore[attr-defined]
                feed = self._client.getfeedToken()  # type: ignore[attr-defined]
                self._tokens = SessionTokens(
                    jwt_token=self._tokens.jwt_token,
                    refresh_token=self._tokens.refresh_token,
                    feed_token=str(feed),
                )
                logger.info("Session token refreshed.")
                return self._tokens
            except Exception as exc:  # noqa: BLE001 - SDK raises broad errors
                logger.warning("Refresh failed (%s); re-logging in.", exc)
                return self._login_with_backoff()

    def is_stale(self, max_age_seconds: float = 6 * 3600) -> bool:
        """True if tokens are older than ``max_age_seconds`` (default 6h)."""
        if self._tokens is None:
            return True
        return (time.time() - self._tokens.issued_at) > max_age_seconds

    # ---- internals ----------------------------------------------------------
    def _credentials(self) -> tuple[str, str, str, str]:
        """Return (api_key, client_code, pin, totp_secret), decrypting if needed."""
        s = self._settings
        if not s.credentials_encrypted:
            return s.api_key, s.client_code, s.pin, s.totp_secret
        key = s.require_encryption_key()
        return (
            crypto.decrypt(s.api_key, key),
            crypto.decrypt(s.client_code, key),
            crypto.decrypt(s.pin, key),
            crypto.decrypt(s.totp_secret, key),
        )

    def _do_login(self) -> SessionTokens:
        """Single login attempt (no retry)."""
        # Lazy import keeps the SDK optional for non-broker code paths/tests.
        from SmartApi import SmartConnect  # type: ignore[import-not-found]

        api_key, client_code, pin, totp_secret = self._credentials()
        totp = pyotp.TOTP(totp_secret).now()

        client = SmartConnect(api_key)
        data = client.generateSession(client_code, pin, totp)
        if not data or not data.get("status"):
            raise RuntimeError(f"Login rejected by SmartAPI: {data}")

        payload = data["data"]
        feed = client.getfeedToken()
        self._client = client
        self._tokens = SessionTokens(
            jwt_token=payload["jwtToken"],
            refresh_token=payload["refreshToken"],
            feed_token=str(feed),
        )
        logger.info("SmartAPI login successful for client %s.", client_code)
        return self._tokens

    def _login_with_backoff(self) -> SessionTokens:
        """Retry login with exponential backoff, honouring settings caps."""
        s = self._settings
        attempt = 0
        delay = s.reconnect_base_delay_sec
        while True:
            attempt += 1
            try:
                return self._do_login()
            except Exception as exc:  # noqa: BLE001
                if s.reconnect_max_retries and attempt >= s.reconnect_max_retries:
                    logger.error("Login failed after %d attempts: %s", attempt, exc)
                    raise
                logger.warning(
                    "Login attempt %d failed (%s). Retrying in %.1fs.",
                    attempt, exc, delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, s.reconnect_max_delay_sec)
