"""Guardrails enforced before any order reaches Bybit."""
import hmac
import threading
import time

from .config import Settings
from .signals import TradeSignal


class SafetyError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class Cooldown:
    """Per-key duplicate suppression, e.g. one trade per (symbol, side) per window.

    TradingView resends an alert up to 3 more times (after ~5s) whenever the
    webhook returns a 5xx. We set the cooldown *before* placing the order so any
    such retry is swallowed here instead of double-trading.
    """

    def __init__(self, seconds: int):
        self.seconds = seconds
        self._last: dict = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        """True if allowed, False if ``key`` is still inside its cooldown window."""
        if self.seconds <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self.seconds:
                return False
            self._last[key] = now
            return True


def check_configuration(settings: Settings) -> None:
    """Fail closed: refuse to trade until the bot is actually configured."""
    if not settings.webhook_secret:
        raise SafetyError("WEBHOOK_SECRET is not configured", 503)
    if not settings.bybit_api_key or not settings.bybit_api_secret:
        raise SafetyError("Bybit API credentials are not configured", 503)


def validate_secret(secret: str, settings: Settings) -> None:
    if not secret or not hmac.compare_digest(secret, settings.webhook_secret):
        raise SafetyError("Unauthorized", 401)


def validate_trading_enabled(settings: Settings) -> None:
    if not settings.trading_enabled:
        raise SafetyError("Trading is disabled (TRADING_ENABLED=false)", 503)


def validate_symbol(symbol: str, settings: Settings) -> None:
    if symbol not in settings.allowed_symbol_set:
        raise SafetyError(f"Symbol {symbol} is not in ALLOWED_SYMBOLS", 400)


def validate_leverage(leverage: int | None, settings: Settings) -> None:
    if leverage is not None and leverage > settings.max_leverage:
        raise SafetyError(
            f"Leverage {leverage}x exceeds MAX_LEVERAGE {settings.max_leverage}x", 400
        )


def validate_tp_sl_side(tp: float | None, sl: float | None, price: float, side: str) -> None:
    """TP/SL prices must sit on the correct side of the market for ``side``.

    Buy (long): take-profit above price, stop-loss below.
    Sell (short): take-profit below price, stop-loss above.

    Last guard against ever sending an inverted stop-loss (e.g. a long-style SL
    attached to a close/reversal order, or a short's SL placed below entry).
    """
    if tp is not None and (tp > price) != (side == "Buy"):
        raise SafetyError(
            f"takeProfit {tp} is on the wrong side for a {side} order at price {price}", 400
        )
    if sl is not None and (sl > price) != (side == "Sell"):
        raise SafetyError(
            f"stopLoss {sl} is on the wrong side for a {side} order at price {price}", 400
        )


def validate_qty(qty: float | None, price: float | None, settings: Settings) -> None:
    if qty is None:
        return
    if qty <= 0:
        raise SafetyError("'qty' must be positive", 400)
    if qty > settings.max_qty_per_order:
        raise SafetyError(
            f"Qty {qty} exceeds MAX_QTY_PER_ORDER {settings.max_qty_per_order}", 400
        )
    if price is not None:
        notional = qty * price
        if notional > settings.max_notional_usd:
            raise SafetyError(
                f"Notional ${notional:,.2f} exceeds MAX_NOTIONAL_USD "
                f"${settings.max_notional_usd:,.2f}",
                400,
            )
