"""Parse TradingView webhook payloads into a normalized TradeSignal."""
import json
from dataclasses import dataclass

from .config import Settings


class SignalError(Exception):
    """Raised when a payload cannot be parsed into a valid signal."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class TradeSignal:
    secret: str
    symbol: str
    side: str                      # "buy" | "sell" | "close"
    qty: float | None = None
    leverage: int | None = None
    tp: float | None = None        # take-profit price
    sl: float | None = None        # stop-loss price
    source_id: str = ""            # idempotency key supplied by the alert

    @property
    def is_close(self) -> bool:
        return self.side == "close"


_SIDE_ALIASES = {
    "buy": "buy", "long": "buy", "open_long": "buy", "open-long": "buy", "openlong": "buy",
    "sell": "sell", "short": "sell", "open_short": "sell", "open-short": "sell", "openshort": "sell",
    "close": "close", "exit": "close",
}


def parse_payload(raw: str) -> dict:
    """Coerce the raw HTTP body into a dict.

    TradingView sends the alert message text verbatim as the request body. It is
    normally a JSON object, but strategy alerts can wrap the real payload in a
    ``message`` string field — both shapes are handled here.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise SignalError("Body is not valid JSON")

    if isinstance(data, dict) and isinstance(data.get("message"), str):
        inner = data["message"].strip()
        if inner:
            try:
                data = json.loads(inner)
            except json.JSONDecodeError:
                raise SignalError("'message' field is not valid JSON")

    if not isinstance(data, dict):
        raise SignalError("Payload must be a JSON object")

    return data


def _first(data: dict, *keys):
    """Return the first key present with a non-None value."""
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _normalize_symbol(raw) -> str:
    """TradingView tickers arrive as ``BINANCE:BTCUSDT.P`` — coerce to ``BTCUSDT``."""
    symbol = str(raw or "").strip()
    if not symbol:
        return ""
    symbol = symbol.upper()
    # Strip TradingView perpetual-futures suffixes (".P", ":P", "_P", "PERP").
    for suffix in (".P", ":P", "_P", "PERP"):
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            symbol = symbol[:-len(suffix)]
            break
    # Strip an "EXCHANGE:" prefix (e.g. "BINANCE:BTCUSDT" -> "BTCUSDT").
    if ":" in symbol:
        symbol = symbol.rsplit(":", 1)[1]
    return symbol


def _as_float(value, field_name: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SignalError(f"'{field_name}' must be a number, got {value!r}")


def _as_int(value, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SignalError(f"'{field_name}' must be an integer, got {value!r}")


def build_signal(data: dict, settings: Settings) -> TradeSignal:
    """Validate and normalize a parsed payload into a TradeSignal.

    Accepted field aliases (TradingView strategy-alert friendly):
      symbol    <- symbol | ticker
      side      <- side | action | direction
      qty       <- qty | quantity | contracts
      tp        <- tp | takeProfit | take_profit | targetPrice
      sl        <- sl | stopLoss | stop_loss | stopPrice
      id        <- id | alert_id
    """
    secret = data.get("secret") or data.get("passphrase") or ""
    if not isinstance(secret, str) or not secret.strip():
        raise SignalError("Missing 'secret' in payload")

    symbol = _normalize_symbol(_first(data, "symbol", "ticker"))
    if not symbol:
        raise SignalError("Missing 'symbol' in payload")

    side_raw = str(_first(data, "side", "action", "direction") or "").strip().lower().replace(" ", "_")
    side = _SIDE_ALIASES.get(side_raw)
    if side is None:
        raise SignalError(
            f"Invalid 'side' {side_raw!r}; expected buy/sell/close (or long/short/exit)"
        )

    qty = _as_float(_first(data, "qty", "quantity", "contracts"), "qty")
    leverage = _as_int(_first(data, "leverage"), "leverage")
    tp = _as_float(_first(data, "tp", "takeProfit", "take_profit", "targetPrice"), "tp")
    sl = _as_float(_first(data, "sl", "stopLoss", "stop_loss", "stopPrice"), "sl")
    source_id = str(_first(data, "id", "alert_id") or "").strip()

    if qty is not None and qty <= 0:
        raise SignalError("'qty' must be positive")
    if leverage is not None and leverage <= 0:
        raise SignalError("'leverage' must be positive")
    if tp is not None and tp <= 0:
        raise SignalError("'tp' must be positive")
    if sl is not None and sl <= 0:
        raise SignalError("'sl' must be positive")

    return TradeSignal(
        secret=secret.strip(),
        symbol=symbol,
        side=side,
        qty=qty,
        leverage=leverage,
        tp=tp,
        sl=sl,
        source_id=source_id,
    )
