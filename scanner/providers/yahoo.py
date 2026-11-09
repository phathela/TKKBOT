"""Yahoo Finance via ``yfinance`` — fallback quotes for stocks.

``yfinance`` is an **optional** dependency (it drags in pandas): the provider
reports itself unavailable when the package is missing, and the scan carries on
with the primary provider. Install it with ``pip install yfinance`` if you want
the fallback.
"""
from __future__ import annotations

import logging

from ..schemas import StockQuote
from .base import Provider

logger = logging.getLogger(__name__)


class YahooProvider(Provider):
    name = "yahoo"

    def available(self) -> bool:
        try:
            import yfinance  # noqa: F401
        except ImportError:
            return False
        return True

    def quotes(self, symbols: list[str]) -> dict[str, StockQuote]:
        if not self.available():
            return {}
        import yfinance as yf  # imported lazily: optional dependency

        result: dict[str, StockQuote] = {}
        for symbol in symbols:
            symbol = symbol.strip().upper()
            if not symbol:
                continue
            try:
                frame = yf.Ticker(symbol).history(period="1mo", interval="1d")
            except Exception as exc:  # noqa: BLE001 — third-party, any failure
                logger.warning("Yahoo quote failed for %s: %s", symbol, exc)
                continue
            if frame is None or len(frame) == 0:
                continue
            try:
                last = frame.iloc[-1]
                result[symbol] = StockQuote(
                    symbol=symbol,
                    name=symbol,
                    price=float(last["Close"]),
                    high=float(last["High"]),
                    low=float(last["Low"]),
                    volume=float(last["Volume"]),
                    avg_volume=float(frame["Volume"].tail(20).mean()),
                    change_pct=None,
                    source=self.name,
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Yahoo row unusable for %s: %s", symbol, exc)
        logger.info("Yahoo fallback: %d quotes", len(result))
        return result
