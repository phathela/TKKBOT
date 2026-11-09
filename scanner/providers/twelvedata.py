"""Twelve Data quotes — the primary stock provider.

``/quote`` returns high, low, volume and ``average_volume`` in one call, which
is exactly what the volatility and volume-spike scores need. Symbols are
batched; the free tier meters *credits per minute*, so chunks are paced by
``TWELVEDATA_CHUNK_DELAY_SECONDS`` (set it to 0 on a paid plan).
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from ..schemas import StockQuote
from .base import HttpClient, Provider, ProviderError

logger = logging.getLogger(__name__)

QUOTE_URL = "https://api.twelvedata.com/quote"


class TwelveDataProvider(Provider):
    name = "twelvedata"

    def __init__(
        self,
        http: HttpClient,
        api_key: str,
        *,
        chunk_size: int = 8,
        chunk_delay_seconds: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(http)
        self._api_key = api_key
        self._chunk_size = max(1, chunk_size)
        self._delay = max(0.0, chunk_delay_seconds)
        self._sleep = sleep

    def quotes(self, symbols: list[str]) -> dict[str, StockQuote]:
        wanted = [s.strip().upper() for s in symbols if s.strip()]
        quotes: dict[str, StockQuote] = {}
        for index in range(0, len(wanted), self._chunk_size):
            chunk = wanted[index:index + self._chunk_size]
            if index and self._delay:
                self._sleep(self._delay)
            quotes.update(self._fetch_chunk(chunk))
        logger.info("Twelve Data: %d/%d quotes", len(quotes), len(wanted))
        return quotes

    def _fetch_chunk(self, chunk: list[str]) -> dict[str, StockQuote]:
        try:
            payload = self.http.get_json(
                QUOTE_URL,
                params={"symbol": ",".join(chunk), "apikey": self._api_key},
            )
        except ProviderError as exc:
            logger.warning("Twelve Data chunk failed: %s", exc)
            return {}

        if not isinstance(payload, dict):
            return {}
        if "code" in payload and "message" in payload:  # whole-request error
            raise ProviderError(f"twelvedata: {payload.get('message')}")

        # A single symbol returns the quote object; several return a mapping.
        rows = payload if len(chunk) > 1 else {chunk[0]: payload}
        result: dict[str, StockQuote] = {}
        for symbol, row in rows.items():
            if not isinstance(row, dict):
                continue
            if "code" in row and "message" in row:
                logger.warning("Twelve Data skipped %s: %s", symbol, row.get("message"))
                continue
            symbol = symbol.strip().upper()
            result[symbol] = StockQuote(
                symbol=symbol,
                name=str(row.get("name") or symbol),
                price=_num(row.get("close")) or 0.0,
                high=_num(row.get("high")),
                low=_num(row.get("low")),
                volume=_num(row.get("volume")),
                avg_volume=_num(row.get("average_volume")),
                change_pct=_num(row.get("percent_change")),
                source=self.name,
                stale=not bool(row.get("is_market_open", True)),
            )
        return result


def _num(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "", "None") else None
    except (TypeError, ValueError):
        return None
