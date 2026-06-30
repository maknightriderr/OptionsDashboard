"""
Central configuration for the option terminal.

Every tunable value lives here and is sourced from environment variables
(or a local .env file). Nothing in this project should hardcode a credential,
a path, or a magic number that an operator might reasonably want to change.

Usage:
    from config.settings import get_settings
    settings = get_settings()          # cached singleton
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application settings loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="OT_",          # OT_ = Option Terminal. e.g. OT_API_KEY
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Angel One SmartAPI credentials -------------------------------------
    # These may be supplied either as plaintext (dev) or ciphertext (prod).
    # When *_ENCRYPTED flags are true, values are decrypted at load time using
    # OT_ENCRYPTION_KEY via utils.crypto.
    api_key: str = Field(..., description="SmartAPI trading API key")
    client_code: str = Field(..., description="Angel One client / login id")
    pin: str = Field(..., description="Account PIN / MPIN")
    totp_secret: str = Field(..., description="Base32 TOTP secret from the authenticator QR")

    credentials_encrypted: bool = Field(
        default=False,
        description="If true, the four credential fields above are Fernet ciphertext.",
    )
    encryption_key: str | None = Field(
        default=None,
        description="Fernet key used to decrypt credentials. Required if credentials_encrypted.",
    )

    # ---- Database -----------------------------------------------------------
    db_backend: Literal["sqlite", "postgres"] = Field(default="sqlite")
    db_path: str = Field(
        default="data/option_terminal.db",
        description="SQLite file path (sqlite backend only).",
    )
    db_dsn: str | None = Field(
        default=None,
        description="PostgreSQL DSN, e.g. postgresql://user:pw@host:5432/db (postgres backend only).",
    )

    # ---- Market / collection parameters -------------------------------------
    # Phase 1 ships NIFTY only; the list keeps the door open for the other three.
    indices: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["NIFTY"])
    strikes_around_atm: int = Field(
        default=15,
        ge=1,
        le=100,
        description="Number of strikes to subscribe on EACH side of ATM (per index).",
    )
    scrip_master_url: str = Field(
        default="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        description="Angel One public instrument master (scrip master) JSON URL.",
    )
    scrip_master_cache: str = Field(
        default="data/scrip_master.json",
        description="Local cache path for the scrip master.",
    )
    scrip_master_max_age_hours: int = Field(default=12, ge=1)

    # ---- Pricing model (Phase 5) --------------------------------------------
    risk_free_rate: float = Field(
        default=0.065,
        ge=0.0, le=0.5,
        description="Annualised risk-free rate used by Black-76 (e.g. 0.065 = 6.5%).",
    )
    dividend_yield: float = Field(
        default=0.012,
        ge=0.0, le=0.2,
        description="Annualised index dividend yield for the forward price.",
    )

    # ---- Alerts / Telegram (Phase 4) ----------------------------------------
    telegram_bot_token: str | None = Field(
        default=None,
        description="Telegram bot token from @BotFather. Encryptable like credentials.",
    )
    telegram_chat_id: str | None = Field(
        default=None,
        description="Destination chat/channel id for pushed alerts.",
    )
    alert_min_priority: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = Field(
        default="MEDIUM",
        description="Signals below this computed priority are stored but not pushed.",
    )
    alert_cooldown_sec: int = Field(
        default=180,
        ge=0,
        description="Per (index, direction, kind) minimum gap between pushed alerts.",
    )

    # ---- Resilience ---------------------------------------------------------
    reconnect_max_retries: int = Field(default=0, description="0 == retry forever")
    reconnect_base_delay_sec: float = Field(default=1.0, ge=0.1)
    reconnect_max_delay_sec: float = Field(default=60.0, ge=1.0)

    # ---- Logging ------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    log_dir: str = Field(default="logs")

    @field_validator("indices", mode="before")
    @classmethod
    def _split_indices(cls, value: object) -> object:
        """Allow OT_INDICES="NIFTY,BANKNIFTY" comma-separated form from env."""
        if isinstance(value, str):
            return [v.strip().upper() for v in value.split(",") if v.strip()]
        return value

    def require_encryption_key(self) -> str:
        """Return the encryption key or raise if credentials are encrypted without one."""
        if self.credentials_encrypted and not self.encryption_key:
            raise ValueError(
                "credentials_encrypted is true but OT_ENCRYPTION_KEY is not set."
            )
        assert self.encryption_key is not None  # for type-checkers
        return self.encryption_key

    def resolve_telegram(self) -> tuple[str | None, str | None]:
        """Return (bot_token, chat_id), decrypting them if credentials are encrypted."""
        token, chat_id = self.telegram_bot_token, self.telegram_chat_id
        if self.credentials_encrypted and token:
            from utils import crypto
            key = self.require_encryption_key()
            token = crypto.decrypt(token, key)
            if chat_id:
                chat_id = crypto.decrypt(chat_id, key)
        return token, chat_id


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance (constructed once per process)."""
    return Settings()  # type: ignore[call-arg]  # values come from env
