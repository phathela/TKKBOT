"""Investing.com — deliberately a disabled stub.

Investing.com has **no official public API**. Scraping it violates its terms of
service and breaks constantly behind Cloudflare, so this project does not do
it. The class exists only so the provider interface has a documented, explicit
"not available" implementation; it is disabled by default and refuses to
return data even if enabled.

The data it would have supplied is covered by the supported sources:
  * movers            -> Bybit tickers (crypto) / Twelve Data quotes (stocks)
  * economic calendar -> Finnhub ``/calendar/economic`` or Twelve Data
  * news sentiment    -> Finnhub company news, NewsAPI, CryptoPanic
"""
from __future__ import annotations

import logging

from .base import Provider, ProviderError

logger = logging.getLogger(__name__)


class InvestingComProvider(Provider):
    name = "investingcom"
    enabled = False

    def available(self) -> bool:
        return False

    def quotes(self, symbols: list[str]) -> dict:  # pragma: no cover - never called
        raise ProviderError(
            "Investing.com has no public API and scraping it violates its terms; "
            "use Twelve Data, Finnhub or Yahoo instead."
        )
