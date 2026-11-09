"""Finnhub — the swappable alternative stock provider, plus company headlines.

``/quote`` is per-symbol and carries current/high/low but **no volume**, so the
average volume comes from ``/stock/metric`` (``10DayAverageTradingVolume``,
reported in millions of shares). Current-day volume is unavailable on the free
tier, which means the volume-spike component is scored as unknown for this
provider — the scan records that rather than inventing a number.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Callable

from ..schemas import Headline, StockQuote
from .base import HttpClient, Provider, ProviderError

logger = logging.getLogger(__name__)

QUOTE_URL = "https://finnhub.io/api/v1/quote"
METRIC_URL = "https://finnhub.io/api/v1/stock/metric"
NEWS_URL = "https://finnhub.io/api/v1/company-news"


class FinnhubProvider(Provider):
    name = "finnhub"

    def __init__(
        self,
        http: HttpClient,
        api_key: str,
        *,
        request_delay_seconds: float = 1.05,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(http)
        self._api_key = api_key
        self._delay = max(0.0, request_delay_seconds)
        self._sleep = sleep

    # -- quotes ------------------------------------------------------------

    def quotes(self, symbols: list[str]) -> dict[str, StockQuote]:
        result: dict[str, StockQuote] = {}
        for index, raw in enumerate(symbols):
            symbol = raw.strip().upper()
            if not symbol:
                continue
            if index and self._delay:
                self._sleep(self._delay)
            try:
                quote = self.http.get_json(
                    QUOTE_URL, params={"symbol": symbol, "token": self._api_key}
                )
            except ProviderError as exc:
                logger.warning("Finnhub quote failed for %s: %s", symbol, exc)
                continue
            if not isinstance(quote, dict) or not quote.get("c"):
                continue
            result[symbol] = StockQuote(
                symbol=symbol,
                name=symbol,
                price=float(quote.get("c") or 0.0),
                high=_num(quote.get("h")),
                low=_num(quote.get("l")),
                volume=None,  # not offered by /quote
                avg_volume=None,
                change_pct=_change(quote.get("c"), quote.get("pc")),
                source=self.name,
            )
        logger.info("Finnhub: %d quotes", len(result))
        return result

    def average_volumes(self, symbols: list[str]) -> dict[str, float]:
        """10-day average daily volume in shares (metric reports millions)."""
        result: dict[str, float] = {}
        for index, raw in enumerate(symbols):
            symbol = raw.strip().upper()
            if not symbol:
                continue
            if self._delay:
                self._sleep(self._delay)
            try:
                payload = self.http.get_json(
                    METRIC_URL,
                    params={"symbol": symbol, "metric": "all", "token": self._api_key},
                )
            except ProviderError as exc:
                logger.warning("Finnhub metric failed for %s: %s", symbol, exc)
                continue
            metrics = (payload or {}).get("metric") or {}
            millions = _num(metrics.get("10DayAverageTradingVolume"))
            if millions is not None:
                result[symbol] = millions * 1_000_000
        return result

    # -- news --------------------------------------------------------------

    def company_headlines(self, symbols: list[str], *, limit: int = 2) -> list[Headline]:
        headlines: list[Headline] = []
        today = date.today()
        start = (today - timedelta(days=2)).isoformat()
        for index, raw in enumerate(symbols):
            symbol = raw.strip().upper()
            if not symbol or len(headlines) >= 12:
                break
            if self._delay:
                self._sleep(self._delay)
            try:
                payload = self.http.get_json(
                    NEWS_URL,
                    params={
                        "symbol": symbol,
                        "from": start,
                        "to": today.isoformat(),
                        "token": self._api_key,
                    },
                )
            except ProviderError as exc:
                logger.warning("Finnhub news failed for %s: %s", symbol, exc)
                continue
            for row in (payload or [])[:limit]:
                title = str(row.get("headline") or "").strip()
                if title:
                    headlines.append(
                        Headline(
                            title=title,
                            source=str(row.get("source") or "finnhub"),
                            url=str(row.get("url") or ""),
                            published_at=_iso(row.get("datetime")),
                        )
                    )
        return headlines


def _num(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _change(current: object, previous: object) -> float | None:
    now, before = _num(current), _num(previous)
    if not now or not before:
        return None
    return (now - before) / before * 100.0


def _iso(epoch: object) -> str:
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""
