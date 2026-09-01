"""Thin wrapper around pybit's unified HTTP client (Bybit V5 REST API).

Trades USDT perpetual futures (``category="linear"``) in one-way position mode.
pybit handles the HMAC-SHA256 request signing; this module only builds requests
and translates responses/errors for the rest of TKKBOT.
"""
import logging
import math
import uuid

from pybit.exceptions import FailedRequestError, InvalidRequestError
from pybit.unified_trading import HTTP

from .config import Settings
from .signals import TradeSignal

logger = logging.getLogger(__name__)


class BybitError(Exception):
    """Any failure talking to Bybit, or a non-zero retCode on an order call."""


class BybitClient:
    def __init__(self, settings: Settings, session=None):
        self.settings = settings
        # ``session`` is injectable so tests can substitute a fake.
        self.session = session or HTTP(
            testnet=settings.bybit_testnet,
            api_key=settings.bybit_api_key,
            api_secret=settings.bybit_api_secret,
        )
        self._qty_step_cache: dict[str, float] = {}

    # ------------------------------------------------------------------ reads

    def get_price(self, symbol: str) -> float | None:
        try:
            resp = self.session.get_tickers(category="linear", symbol=symbol)
            return float(resp["result"]["list"][0]["lastPrice"])
        except Exception:  # noqa: BLE001
            logger.exception("Failed to fetch price for %s", symbol)
            return None

    def get_balance(self, coin: str = "USDT") -> float:
        try:
            resp = self.session.get_wallet_balance(accountType="UNIFIED", coin=coin)
            return float(resp["result"]["list"][0]["coin"][0]["walletBalance"])
        except Exception:  # noqa: BLE001
            logger.exception("Failed to fetch wallet balance")
            return 0.0

    def get_position(self, symbol: str) -> dict | None:
        """Return the current one-way position for ``symbol``, or None when flat.

        Result shape: ``{"side": "Buy"|"Sell", "size": float, "avg_price": float}``.
        """
        try:
            resp = self.session.get_positions(category="linear", symbol=symbol)
            for pos in resp["result"]["list"]:
                if pos.get("positionIdx") == 0 and float(pos.get("size") or 0) > 0:
                    return {
                        "side": pos["side"],
                        "size": float(pos["size"]),
                        "avg_price": float(pos.get("avgPrice") or 0),
                    }
            return None
        except Exception:  # noqa: BLE001
            logger.exception("Failed to fetch position for %s", symbol)
            return None

    def get_qty_step(self, symbol: str) -> float:
        """The symbol's minimum qty increment (``lotSizeFilter.qtyStep``), cached."""
        step = self._qty_step_cache.get(symbol)
        if step is not None:
            return step
        try:
            resp = self.session.get_instruments_info(category="linear", symbol=symbol)
            step = float(resp["result"]["list"][0]["lotSizeFilter"]["qtyStep"])
            self._qty_step_cache[symbol] = step
            return step
        except Exception as e:  # noqa: BLE001
            raise BybitError(f"Could not fetch qty step for {symbol}: {e}") from e

    # ----------------------------------------------------------------- writes

    def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set buy/sell leverage. retCode 110043 ('leverage not changed') is benign."""
        try:
            resp = self.session.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
        except (InvalidRequestError, FailedRequestError) as e:
            raise BybitError(f"set_leverage failed for {symbol}: {e}") from e
        if str(resp.get("retCode")) == "110043":
            logger.info("Leverage for %s already %sx (110043 ignored)", symbol, leverage)
            return
        self._raise_if_error(resp, "set_leverage")

    def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        leverage: int | None = None,
        tp: float | None = None,
        sl: float | None = None,
    ) -> dict:
        """Open/adjust a market order with optional leverage and attached TP/SL."""
        if leverage is not None:
            self.set_leverage(symbol, leverage)

        qty_rounded = self._round_qty_to_step(symbol, qty)
        if qty_rounded <= 0:
            raise BybitError(
                f"Qty {qty} is below the minimum lot step {self.get_qty_step(symbol)} for {symbol}"
            )
        if qty_rounded != qty:
            logger.info("Rounded qty %s -> %s for %s (lot step)", qty, qty_rounded, symbol)

        params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": format_qty(qty_rounded),
            "timeInForce": "IOC",
            "positionIdx": 0,
            "orderLinkId": f"tkkbot_{uuid.uuid4().hex[:12]}",
        }
        if tp is not None:
            params["takeProfit"] = format_price(tp)
        if sl is not None:
            params["stopLoss"] = format_price(sl)
        if tp is not None or sl is not None:
            params["tpslMode"] = "Partial"

        resp = self._call("place_order", params)
        result = resp.get("result", {})
        logger.info(
            "Order placed: symbol=%s side=%s qty=%s tp=%s sl=%s orderId=%s",
            symbol, side, qty_rounded, tp, sl, result.get("orderId"),
        )
        return {"order_id": result.get("orderId"), "order_link_id": params["orderLinkId"]}

    def close_position(self, symbol: str, qty: float | None = None) -> dict:
        """Reduce-only close of the current one-way position (exchange is source of truth)."""
        position = self.get_position(symbol)
        if position is None:
            raise BybitError(f"No open position to close for {symbol}")
        close_qty = position["size"] if qty is None else min(qty, position["size"])
        close_qty = self._round_qty_to_step(symbol, close_qty)
        if close_qty <= 0:
            raise BybitError(f"No open position to close for {symbol}")
        side = "Sell" if position["side"] == "Buy" else "Buy"

        params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": format_qty(close_qty),
            "timeInForce": "IOC",
            "positionIdx": 0,
            "reduceOnly": True,
            "orderLinkId": f"tkkbot_{uuid.uuid4().hex[:12]}",
        }
        resp = self._call("place_order", params)
        result = resp.get("result", {})
        logger.info(
            "Position closed: symbol=%s side=%s qty=%s orderId=%s",
            symbol, side, close_qty, result.get("orderId"),
        )
        return {"order_id": result.get("orderId"), "closed_qty": close_qty}

    def compute_tp_sl(self, signal: TradeSignal, price: float) -> tuple[float | None, float | None]:
        """Resolve (tp, sl) prices for an entry: explicit alert values win, else percent defaults."""
        tp = signal.tp
        sl = signal.sl
        if tp is None and self.settings.default_tp_percent > 0:
            tp = price * (1 + self.settings.default_tp_percent) if signal.side == "buy" \
                else price * (1 - self.settings.default_tp_percent)
        if sl is None and self.settings.default_sl_percent > 0:
            sl = price * (1 - self.settings.default_sl_percent) if signal.side == "buy" \
                else price * (1 + self.settings.default_sl_percent)
        return tp, sl

    # ----------------------------------------------------------------- helpers

    def _call(self, method: str, params: dict) -> dict:
        try:
            resp = getattr(self.session, method)(**params)
        except (InvalidRequestError, FailedRequestError) as e:
            raise BybitError(f"{method} failed: {e}") from e
        self._raise_if_error(resp, method)
        return resp

    @staticmethod
    def _raise_if_error(resp: dict, action: str) -> None:
        if str(resp.get("retCode")) != "0":
            raise BybitError(f"{action} failed (retCode={resp.get('retCode')}): {resp.get('retMsg')}")

    def _round_qty_to_step(self, symbol: str, qty: float) -> float:
        step = self.get_qty_step(symbol)
        if step <= 0:
            return qty
        return math.floor(qty / step + 1e-9) * step


def format_qty(qty: float) -> str:
    """Format a qty float as Bybit expects (decimal string, trailing zeros stripped)."""
    return ("%.8f" % qty).rstrip("0").rstrip(".") or "0"


def format_price(price: float) -> str:
    return ("%.6f" % price).rstrip("0").rstrip(".") or "0"
