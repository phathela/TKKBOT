"""TKKBOT configuration, loaded from environment variables.

Every field maps to an environment variable of the same name (upper-cased),
e.g. ``BYBIT_API_KEY`` -> ``bybit_api_key``. See ``.env.example``.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Bybit credentials ---
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = False

    # --- Webhook auth ---
    webhook_secret: str = ""

    # --- Safety guardrails ---
    trading_enabled: bool = True
    allowed_symbols: str = "BTCUSDT"
    max_qty_per_order: float = 100.0
    max_notional_usd: float = 100000.0
    max_leverage: int = 5
    margin_usage_percent: float = 0.90  # auto-size uses 90% of wallet balance as margin

    # --- Defaults applied when an alert omits values ---
    default_leverage: int = 5
    default_tp_percent: float = 0.0   # 0 = do not attach a take-profit
    default_sl_percent: float = 0.04  # 4% price move = 20% of margin at 5x leverage

    # --- Behaviour ---
    cooldown_seconds: int = 5
    log_level: str = "INFO"
    port: int = 8000

    @property
    def allowed_symbol_set(self) -> set[str]:
        return {s.strip().upper() for s in self.allowed_symbols.split(",") if s.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
