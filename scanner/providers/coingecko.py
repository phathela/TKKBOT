"""CoinGecko markets — the primary crypto listing (mcap, volume, 24h range)."""
from __future__ import annotations

import logging

from ..schemas import CryptoMarket
from .base import HttpClient, Provider, ProviderError

logger = logging.getLogger(__name__)

MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"


class CoinGeckoProvider(Provider):
    name = "coingecko"

    def __init__(self, http: HttpClient, api_key: str) -> None:
        super().__init__(http)
        self._api_key = api_key

    def markets(self, *, limit: int = 250) -> list[CryptoMarket]:
        payload = self.http.get_json(
            MARKETS_URL,
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": min(max(limit, 1), 250),
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h,7d,30d",
            },
            headers={"x-cg-demo-api-key": self._api_key},
        )
        if not isinstance(payload, list):
            raise ProviderError("coingecko returned an unexpected payload")

        markets: list[CryptoMarket] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            markets.append(
                CryptoMarket(
                    symbol=symbol,
                    name=str(row.get("name") or symbol),
                    price=_num(row.get("current_price")) or 0.0,
                    high_24h=_opt(row.get("high_24h")),
                    low_24h=_opt(row.get("low_24h")),
                    volume_24h=_num(row.get("total_volume")) or 0.0,
                    market_cap=_num(row.get("market_cap")) or 0.0,
                    change_24h=_opt(row.get("price_change_percentage_24h")),
                    change_7d=_opt(row.get("price_change_percentage_7d_in_currency")),
                    change_30d=_opt(row.get("price_change_percentage_30d_in_currency")),
                    source=self.name,
                )
            )
        logger.info("CoinGecko: %d markets", len(markets))
        return markets


def _num(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _opt(value: object) -> float | None:
    number = _num(value)
    return number if number is not None else None
