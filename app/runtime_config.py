"""Dashboard-tunable trading settings with optional file persistence.

Everything the user can adjust from the dashboard — leverage, stop-loss %,
take-profit %, margin %, allowed pairs, trading on/off — lives in a single
``RuntimeConfig``. It is seeded from the env ``Settings`` at boot, can be
changed at runtime, and when ``TKKBOT_CONFIG_PATH`` points at a writable file
(a Railway volume mount) it is saved atomically after every change so it
survives restarts and redeploys.

The dashboard is the source of truth for these bot-wide parameters: the
webhook reads them here on every alert rather than from the frozen env object.
"""
import json
import logging
import os
import re
import tempfile
import threading

from .config import Settings

logger = logging.getLogger(__name__)

# USDT perpetual tickers look like BTCUSDT / ETHUSDT / 1000PEPEUSDT.
_SYMBOL_RE = re.compile(r"^[A-Z0-9]+USDT$")


class ConfigError(Exception):
    """Raised for an invalid dashboard update; carries a user-safe message."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _num(value, name: str, lo: float, hi: float, kind) -> float:
    """Coerce ``value`` to int/float and range-check it, else raise ConfigError."""
    if kind is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"'{name}' must be an integer")
        v = value
    else:
        if isinstance(value, bool):
            raise ConfigError(f"'{name}' must be a number")
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"'{name}' must be a number")
    if not (lo <= v <= hi):
        raise ConfigError(f"'{name}' must be between {lo} and {hi}")
    return v


def _clean_symbol(value, name: str) -> str:
    symbol = str(value or "").strip().upper()
    if not _SYMBOL_RE.match(symbol):
        raise ConfigError(
            f"{name}: {value!r} is not a valid USDT perpetual symbol"
        )
    return symbol


def _as_list(value, name: str) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    raise ConfigError(f"'{name}' must be a list of symbols")


class RuntimeConfig:
    def __init__(self, settings: Settings, path: str = "", client=None):
        # ``path`` wins over the env var; empty string = in-memory only.
        self._path = path or settings.tkkbot_config_path or ""
        self._client = client  # optional; used to verify new pairs against Bybit
        self._lock = threading.RLock()
        self.max_leverage = settings.max_leverage

        # Seed from env defaults, then overlay any persisted file.
        self.leverage: int = settings.default_leverage
        self.sl_percent: float = settings.default_sl_percent
        self.tp_percent: float = settings.default_tp_percent
        self.margin_percent: float = settings.margin_usage_percent
        self.trading_enabled: bool = settings.trading_enabled
        self.allowed: set[str] = set(settings.allowed_symbol_set)
        self._load()

    # ------------------------------------------------------------- duck typing
    @property
    def allowed_symbol_set(self) -> set[str]:
        """Matches ``Settings.allowed_symbol_set`` so ``safety.validate_symbol`` works."""
        with self._lock:
            return set(self.allowed)

    @property
    def persistence_path(self) -> str | None:
        return self._path or None

    # ------------------------------------------------------------------ state
    def _snapshot(self) -> dict:
        return {
            "leverage": self.leverage,
            "sl_percent": self.sl_percent,
            "tp_percent": self.tp_percent,
            "margin_percent": self.margin_percent,
            "trading_enabled": self.trading_enabled,
            "allowed_symbols": sorted(self.allowed),
        }

    def to_dict(self) -> dict:
        with self._lock:
            snap = self._snapshot()
        return {
            **snap,
            "max_leverage": self.max_leverage,
            "persistence_path": self.persistence_path,
        }

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        if not self._path:
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return  # first run: keep env defaults
        except (OSError, ValueError) as e:
            logger.warning("TKKBOT config file unreadable, using env defaults: %s", e)
            return
        if not isinstance(data, dict):
            logger.warning("TKKBOT config file has no object root, using env defaults")
            return

        cand = self._snapshot()
        try:
            if isinstance(data.get("leverage"), int):
                cand["leverage"] = _num(data["leverage"], "leverage", 1, self.max_leverage, int)
            if isinstance(data.get("sl_percent"), (int, float)):
                cand["sl_percent"] = _num(data["sl_percent"], "sl_percent", 0.001, 0.5, float)
            if isinstance(data.get("tp_percent"), (int, float)):
                cand["tp_percent"] = _num(data["tp_percent"], "tp_percent", 0.0, 0.5, float)
            if isinstance(data.get("margin_percent"), (int, float)):
                cand["margin_percent"] = _num(
                    data["margin_percent"], "margin_percent", 0.05, 1.0, float
                )
            if isinstance(data.get("trading_enabled"), bool):
                cand["trading_enabled"] = data["trading_enabled"]
            if isinstance(data.get("allowed_symbols"), list):
                symbols = {
                    _clean_symbol(s, "allowed_symbols") for s in data["allowed_symbols"]
                    if isinstance(s, str)
                }
                cand["allowed_symbols"] = sorted(symbols)
        except ConfigError as e:
            logger.warning("Ignoring out-of-range value in TKKBOT config file: %s", e.message)
            return
        self._commit(cand)

    # ------------------------------------------------------------------ save
    def _write(self, cand: dict) -> None:
        """Atomically persist the candidate snapshot; raises ConfigError on failure."""
        directory = os.path.dirname(self._path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tkkbot_cfg_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(cand, fh, indent=2, sort_keys=True)
                os.replace(tmp, self._path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except ConfigError:
            raise
        except OSError as e:
            raise ConfigError(f"Could not save settings: {e}")

    def _commit(self, cand: dict) -> None:
        self.leverage = int(cand["leverage"])
        self.sl_percent = float(cand["sl_percent"])
        self.tp_percent = float(cand["tp_percent"])
        self.margin_percent = float(cand["margin_percent"])
        self.trading_enabled = bool(cand["trading_enabled"])
        self.allowed = set(cand["allowed_symbols"])

    # ----------------------------------------------------------------- update
    def update(self, payload: dict, client=None) -> dict:
        """Apply a partial dashboard update, persist it, and return the new state."""
        verifier = client or self._client
        with self._lock:
            cand = self._snapshot()
            self._apply_changes(cand, payload, verifier)
            if self._path:
                self._write(cand)
            self._commit(cand)
            return self.to_dict()

    def _apply_changes(self, cand: dict, payload: dict, client) -> None:
        if not isinstance(payload, dict):
            raise ConfigError("Body must be a JSON object")

        if "leverage" in payload:
            cand["leverage"] = _num(
                payload["leverage"], "leverage", 1, self.max_leverage, int
            )
        if "sl_percent" in payload:
            cand["sl_percent"] = _num(payload["sl_percent"], "sl_percent", 0.001, 0.5, float)
        if "tp_percent" in payload:
            cand["tp_percent"] = _num(payload["tp_percent"], "tp_percent", 0.0, 0.5, float)
        if "margin_percent" in payload:
            cand["margin_percent"] = _num(
                payload["margin_percent"], "margin_percent", 0.05, 1.0, float
            )
        if "trading_enabled" in payload:
            enabled = payload["trading_enabled"]
            if not isinstance(enabled, bool):
                raise ConfigError("'trading_enabled' must be a boolean")
            cand["trading_enabled"] = enabled

        allowed = set(cand["allowed_symbols"])
        for raw in _as_list(payload.get("pairs_remove"), "pairs_remove"):
            allowed.discard(_clean_symbol(raw, "pairs_remove"))
        for raw in _as_list(payload.get("pairs_add"), "pairs_add"):
            symbol = _clean_symbol(raw, "pairs_add")
            if symbol in allowed:
                continue
            if client is not None:
                if not client.pair_exists(symbol):
                    raise ConfigError(
                        f"{symbol} is not a tradable Bybit USDT perpetual pair"
                    )
            allowed.add(symbol)
        cand["allowed_symbols"] = sorted(allowed)
