"""Scanner configuration, loaded from environment variables.

Mirrors :mod:`app.config`: every field maps to the upper-cased env var of the
same name (``DEEPSEEK_API_KEY`` -> ``deepseek_api_key``). Secrets are never
logged; :meth:`ScanSettings.missing_required` returns the *names* only.
"""
from __future__ import annotations

import logging
import os
from datetime import time
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_DIR = _PACKAGE_DIR.parent


class ConfigError(RuntimeError):
    """Configuration is incomplete or unusable; the scan must not start."""


class ScanSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM (required) ---
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"

    # --- Market data (required) ---
    coingecko_api_key: str = ""
    cmc_api_key: str = ""
    # Stocks: Twelve Data is primary, Finnhub is the swappable alternative.
    twelvedata_api_key: str = ""
    finnhub_api_key: str = ""

    # --- Optional news ---
    news_api_key: str = ""
    cryptopanic_api_key: str = ""

    # --- Delivery ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Scheduling / API ---
    scan_times_utc: str = "00:00,06:00,12:00,18:00"
    scan_scheduler_enabled: bool = False
    opportunities_api_key: str = ""
    # Reserved stub: no trading behaviour is implemented behind this flag.
    opportunities_feed_to_bot: bool = False

    # --- Storage ---
    database_url: str = ""
    scan_db_path: str = ""
    scan_data_dir: str = ""

    # --- Universe thresholds ---
    min_crypto_mcap_usd: float = 100_000_000
    min_crypto_volume_usd: float = 10_000_000
    min_stock_avg_volume: float = 1_000_000

    # --- Pipeline sizes ---
    crypto_universe_limit: int = 250
    crypto_enrich_limit: int = 40  # Bybit candle calls per scan (ATR, volume spike)
    stock_candidate_limit: int = 15
    crypto_candidate_limit: int = 15
    final_crypto: int = 5
    final_stocks: int = 5

    # --- Provider tuning ---
    scan_http_timeout: float = 15.0
    scan_http_retries: int = 2
    twelvedata_chunk_size: int = 8
    twelvedata_chunk_delay_seconds: float = 60.0
    finnhub_request_delay_seconds: float = 1.05

    # --- Files ---
    tech_universe_path: str = ""
    scoring_weights_path: str = ""

    # --- Behaviour ---
    log_level: str = "INFO"

    # ---- derived helpers -------------------------------------------------

    def scan_times(self) -> list[time]:
        """Parse ``SCAN_TIMES_UTC`` ("00:00,06:00,...") into sorted times."""
        parsed: list[time] = []
        for raw in self.scan_times_utc.split(","):
            part = raw.strip()
            if not part:
                continue
            try:
                hour, minute = part.split(":")
                parsed.append(time(hour=int(hour), minute=int(minute)))
            except (ValueError, TypeError) as exc:
                raise ConfigError(
                    f"SCAN_TIMES_UTC contains an invalid time {part!r}; use HH:MM"
                ) from exc
        if not parsed:
            raise ConfigError("SCAN_TIMES_UTC must list at least one HH:MM time")
        return sorted(parsed)

    def data_dir(self) -> Path:
        if self.scan_data_dir:
            return Path(self.scan_data_dir)
        # On Railway the volume is mounted at /data; locally keep it in-repo.
        if Path("/data").is_dir():
            return Path("/data")
        return _REPO_DIR / "data"

    def db_path(self) -> Path | None:
        """Resolve the SQLite path; ``None`` means "use JSON files"."""
        if self.scan_db_path:
            return Path(self.scan_db_path)
        url = self.database_url.strip()
        if url.startswith("sqlite:///"):
            return Path(url[len("sqlite:///"):])
        if url.startswith("sqlite://"):
            return Path(url[len("sqlite://"):] or "scanner.sqlite")
        if url:
            # No psycopg in the image on purpose: keep the deploy light.
            logger.warning(
                "DATABASE_URL uses an unsupported scheme; falling back to SQLite "
                "in %s (only sqlite:// URLs are implemented)", self.data_dir()
            )
            return self.data_dir() / "opportunities.db"
        return None

    def tech_universe_file(self) -> Path:
        return Path(self.tech_universe_path) if self.tech_universe_path \
            else _PACKAGE_DIR / "tech_universe.json"

    def scoring_weights_file(self) -> Path:
        return Path(self.scoring_weights_path) if self.scoring_weights_path \
            else _PACKAGE_DIR / "scoring_weights.json"

    def missing_required(self, *, dry_run: bool = False) -> list[str]:
        """Names of required settings that are unset (never the values)."""
        if dry_run:
            return []
        required = ["DEEPSEEK_API_KEY", "COINGECKO_API_KEY", "CMC_API_KEY"]
        missing = [name for name in required if not getattr(self, name.lower())]
        if not self.twelvedata_api_key and not self.finnhub_api_key:
            missing.append("TWELVEDATA_API_KEY or FINNHUB_API_KEY")
        return missing

    def ensure_ready(self, *, dry_run: bool = False) -> None:
        missing = self.missing_required(dry_run=dry_run)
        if missing:
            raise ConfigError(
                "Scanner is not configured: missing " + ", ".join(missing)
            )
        self.scan_times()  # validates the schedule string


@lru_cache
def get_scan_settings() -> ScanSettings:
    return ScanSettings()


def reset_settings_cache() -> None:
    """Drop the cached settings (used by tests)."""
    get_scan_settings.cache_clear()


def log_settings_summary(settings: ScanSettings) -> None:
    """Log which sources are usable, by boolean only — never key values."""
    logger.info(
        "Scanner config: coingecko=%s cmc=%s twelvedata=%s finnhub=%s "
        "newsapi=%s cryptopanic=%s telegram=%s scheduler=%s times=%s",
        _on(settings.coingecko_api_key),
        _on(settings.cmc_api_key),
        _on(settings.twelvedata_api_key),
        _on(settings.finnhub_api_key),
        _on(settings.news_api_key),
        _on(settings.cryptopanic_api_key),
        _on(settings.telegram_bot_token and settings.telegram_chat_id),
        settings.scan_scheduler_enabled,
        settings.scan_times_utc,
    )


def _on(value: object) -> str:
    return "on" if value else "off"


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}
