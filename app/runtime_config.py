"""Dashboard-tunable trading settings with optional file persistence.

Everything the user can adjust from the dashboard — leverage, stop-loss %,
take-profit %, margin %, allowed pairs, trading on/off — lives in a single
``RuntimeConfig``. It is seeded from the env ``Settings`` at boot, can be
changed at runtime, and when ``TKKBOT_CONFIG_PATH`` points at a writable file
(a Railway volume mount) it is saved atomically after every change so it
survives restarts and redeploys.

The dashboard is the source of truth for these bot-wide parameters: the
webhook reads them here on every alert rather than from the frozen env object.

Alongside the allowlist sits ``pair_meta``: a per-symbol label (so a trade is
identifiable on Bybit and in the logs) and an optional saved base-unit
quantity. The saved quantity exists because the global auto-size commits
``margin_percent`` of the *whole* wallet per alert, which cannot fund a second
pair — see ``pair_qty``.
"""
import json
import logging
import math
import os
import re
import tempfile
import threading

from .config import Settings

logger = logging.getLogger(__name__)

# USDT perpetual tickers look like BTCUSDT / ETHUSDT / 1000PEPEUSDT.
_SYMBOL_RE = re.compile(r"^[A-Z0-9]+USDT$")

# A pair's short name, e.g. "btc-scalp". Kept to 20 chars because it is embedded
# in Bybit's orderLinkId, which is capped at 36 (see ``build_order_link_id``).
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,19}$")

# Fields accepted inside one ``pair_meta`` entry. Unknown keys are rejected so a
# typo'd dashboard field is reported instead of silently ignored.
_META_KEYS = frozenset({"label", "qty"})


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


def _clean_label(value, name: str) -> str:
    """Normalise a pair label to lowercase and validate its shape."""
    label = str(value or "").strip().lower()
    if not _LABEL_RE.match(label):
        raise ConfigError(
            f"{name}: {value!r} must be 1-20 characters of a-z, 0-9, '-' or '_', "
            "and start with a letter or digit"
        )
    return label


def _finite_qty(value, name: str) -> float:
    """Validate a saved quantity: a finite number greater than zero."""
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be a number greater than 0")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a number greater than 0")
    if not math.isfinite(v) or v <= 0:
        raise ConfigError(f"{name} must be a finite number greater than 0")
    return v


def _effective_label(meta: dict, symbol: str) -> str:
    """A pair's label, falling back to its lower-cased symbol."""
    return (meta.get(symbol) or {}).get("label") or symbol.lower()


def _label_conflict(meta: dict, allowed) -> tuple[str, str, str] | None:
    """Find two allowed pairs that would share one effective label.

    Returns ``(symbol, label, owner)`` for the pair that loses, or ``None``.
    Uniqueness is a readability rule — Bybit order IDs carry their own random
    suffix — so it may refuse a *save* but must never refuse a trade.
    """
    seen: dict[str, str] = {}
    for symbol in sorted(allowed):
        label = _effective_label(meta, symbol)
        owner = seen.get(label)
        if owner is not None:
            return symbol, label, owner
        seen[label] = symbol
    return None


def _merge_meta(current: dict, raw, allowed) -> dict:
    """Apply a dashboard ``pair_meta`` update onto ``current``.

    Fields merge rather than replacing the whole entry, so saving a quantity
    from the dashboard cannot silently wipe the label sitting next to it in a
    form the user only half filled in. ``null`` — or an empty string, which is
    what an emptied input sends — clears one field; ``null`` in place of an
    entry drops the pair's record entirely.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("'pair_meta' must be an object keyed by symbol")
    keep = set(allowed)
    meta = {s: dict(m) for s, m in current.items()}
    for key, entry in raw.items():
        symbol = _clean_symbol(key, "pair_meta")
        if symbol not in keep:
            raise ConfigError(f"pair_meta: {symbol} is not one of the allowed pairs")
        if entry is None:
            meta.pop(symbol, None)
            continue
        if not isinstance(entry, dict):
            raise ConfigError(f"pair_meta[{symbol}] must be an object or null")
        unknown = sorted(set(entry) - _META_KEYS)
        if unknown:
            raise ConfigError(
                f"pair_meta[{symbol}]: unknown field(s) {', '.join(unknown)}"
            )
        fields = dict(meta.get(symbol) or {})
        for field, check in (("label", _clean_label), ("qty", _finite_qty)):
            if field not in entry:
                continue
            value = entry[field]
            if value is None or (isinstance(value, str) and not value.strip()):
                fields.pop(field, None)
                continue
            fields[field] = check(value, f"pair_meta[{symbol}].{field}")
        if fields:
            meta[symbol] = fields
        else:
            meta.pop(symbol, None)
    return meta


def _prune_meta(meta: dict, allowed) -> dict:
    """Drop ``pair_meta`` entries for symbols that are not allowed.

    Applied on both snapshot and commit, so "meta is a subset of allowed" holds
    by construction on every write — a hand-edited file or a partially applied
    update cannot leave an entry behind for a pair that was never allowed.
    """
    keep = set(allowed)
    return {s: dict(m) for s, m in meta.items() if s in keep}


def _load_meta(raw, allowed) -> dict:
    """Read persisted ``pair_meta``, skipping anything malformed.

    Never raises — see the call site in ``_load`` for why that matters. Each bad
    entry is dropped on its own so the rest of the file still applies.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("Ignoring non-object pair_meta in TKKBOT config file")
        return {}
    keep = set(allowed)
    meta: dict[str, dict] = {}
    for key, entry in raw.items():
        if not isinstance(key, str):
            continue
        symbol = key.strip().upper()
        if symbol not in keep:
            continue
        if not isinstance(entry, dict):
            logger.warning("Ignoring pair_meta[%s]: not an object", symbol)
            continue
        clean: dict = {}
        for field, check in (("label", _clean_label), ("qty", _finite_qty)):
            if entry.get(field) is None:
                continue
            try:
                clean[field] = check(entry[field], f"pair_meta[{symbol}].{field}")
            except ConfigError as e:
                logger.warning("Ignoring %s", e.message)
        if clean:
            meta[symbol] = clean

    # Resolve duplicate labels by dropping the loser's, never by refusing to
    # load: a collision is a naming problem, and the random order-ID suffix
    # still keeps the two pairs' orders apart on Bybit.
    while True:
        conflict = _label_conflict(meta, keep)
        if conflict is None:
            return meta
        symbol, label, owner = conflict
        logger.warning(
            "Pair label %r is used by both %s and %s; %s falls back to its symbol",
            label, owner, symbol, symbol,
        )
        meta[symbol].pop("label", None)
        if not meta[symbol]:
            del meta[symbol]


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
        # Per-symbol {"label": str, "qty": float}; empty until the user names a
        # pair, so an unlabelled pair still works exactly as it did before.
        self.pair_meta: dict[str, dict] = {}
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
        allowed = sorted(self.allowed)
        return {
            "leverage": self.leverage,
            "sl_percent": self.sl_percent,
            "tp_percent": self.tp_percent,
            "margin_percent": self.margin_percent,
            "trading_enabled": self.trading_enabled,
            "allowed_symbols": allowed,
            "pair_meta": _prune_meta(self.pair_meta, allowed),
        }

    def to_dict(self) -> dict:
        with self._lock:
            snap = self._snapshot()
        return {
            **snap,
            "max_leverage": self.max_leverage,
            "persistence_path": self.persistence_path,
        }

    def pair_qty(self, symbol: str) -> float | None:
        """Saved base-unit quantity for ``symbol``; ``None`` means auto-size."""
        with self._lock:
            return (self.pair_meta.get(symbol.upper()) or {}).get("qty")

    def pair_label(self, symbol: str) -> str:
        """Short name for ``symbol``, used in logs and Bybit order IDs."""
        with self._lock:
            return _effective_label(self.pair_meta, symbol.upper())

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
        # Parsed outside the block above on purpose. That block is
        # all-or-nothing: a ConfigError there discards the whole file and
        # reverts every setting to env defaults, so one typo in a pair label
        # must not also reset the user's leverage and stop-loss.
        cand["pair_meta"] = _load_meta(data.get("pair_meta"), cand["allowed_symbols"])
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
        # Pruned on the way in as well as on the way out, so removing a pair
        # drops its label and saved quantity immediately.
        self.pair_meta = _prune_meta(cand.get("pair_meta") or {}, self.allowed)

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

        # Meta is applied *after* the pair edits and validated against the
        # resulting allowlist, so naming a pair and adding it in one request
        # works. It is also validated against the *new* allowlist, so a pair
        # being removed in this same request cannot be given a label.
        if "pair_meta" in payload:
            cand["pair_meta"] = _merge_meta(
                cand["pair_meta"], payload["pair_meta"], allowed
            )
        conflict = _label_conflict(cand["pair_meta"], allowed)
        if conflict is not None:
            symbol, label, owner = conflict
            raise ConfigError(
                f"Label '{label}' is already used by {owner} — every pair "
                "needs its own name"
            )
