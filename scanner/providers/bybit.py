"""Bybit public market data — no API key, no signing.

Used to enrich the crypto shortlist with the exchange's own 24h ticker, order
book depth (a tradability proxy) and daily candles (ATR). The user trades on
Bybit, so its numbers are the ones that matter at execution time.
"""
from __future__ import annotations

import logging

from .base import HttpClient, Provider, ProviderError

logger = logging.getLogger(__name__)

TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
KLINE_URL = "https://api.bybit.com/v5/market/kline"
ORDERBOOK_URL = "https://api.bybit.com/v5/market/orderbook"


class BybitProvider(Provider):
    name = "bybit"

    def tickers(self, category: str = "linear") -> dict[str, dict]:
        payload = self.http.get_json(TICKERS_URL, params={"category": category})
        rows = (((payload or {}).get("result") or {}).get("list") or [])
        if not isinstance(rows, list):
            raise ProviderError("bybit returned an unexpected ticker payload")
        result = {
            str(row.get("symbol", "")).upper(): row
            for row in rows
            if isinstance(row, dict) and row.get("symbol")
        }
        logger.info("Bybit: %d %s tickers", len(result), category)
        return result

    def klines(self, symbol: str, *, interval: str = "D", limit: int = 20) -> list[dict]:
        payload = self.http.get_json(
            KLINE_URL,
            params={
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "limit": max(2, min(limit, 200)),
            },
        )
        rows = (((payload or {}).get("result") or {}).get("list") or [])
        # Bybit returns newest-first; callers (ATR) want oldest-first.
        return list(reversed(rows)) if isinstance(rows, list) else []

    def depth_usd(self, symbol: str, *, limit: int = 25) -> float | None:
        """Rough top-of-book liquidity in USD (sum of price × size both sides)."""
        payload = self.http.get_json(
            ORDERBOOK_URL,
            params={"category": "linear", "symbol": symbol, "limit": max(5, min(limit, 50))},
        )
        result = (payload or {}).get("result") or {}
        total = 0.0
        for side in ("b", "a"):
            for level in result.get(side) or []:
                try:
                    price, size = float(level[0]), float(level[1])
                except (TypeError, ValueError, IndexError):
                    continue
                total += price * size
        return total or None
