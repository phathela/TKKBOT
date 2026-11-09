"""CoinMarketCap listings — used to cross-check CoinGecko's figures.

CMC's ``listings/latest`` has no 24h high/low, so it never replaces CoinGecko's
price data. It fills gaps in the 7d/30d changes and flags material disagreements
so the stored scan records what the sources did and did not agree on.
"""
from __future__ import annotations

import logging

from ..schemas import CryptoMarket
from .base import HttpClient, Provider, ProviderError

logger = logging.getLogger(__name__)

LISTINGS_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"

# Below this relative gap the two sources are treated as agreeing.
DISAGREEMENT_THRESHOLD = 0.5  # 50%


class CoinMarketCapProvider(Provider):
    name = "coinmarketcap"

    def __init__(self, http: HttpClient, api_key: str) -> None:
        super().__init__(http)
        self._api_key = api_key

    def listings(self, *, limit: int = 250) -> dict[str, dict]:
        payload = self.http.get_json(
            LISTINGS_URL,
            params={"start": 1, "limit": min(max(limit, 1), 500), "convert": "USD"},
            headers={"X-CMC_PRO_API_KEY": self._api_key, "Accept": "application/json"},
        )
        rows = ((payload or {}).get("data") or []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise ProviderError("coinmarketcap returned an unexpected payload")
        result: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol:
                result[symbol] = row
        logger.info("CoinMarketCap: %d listings", len(result))
        return result

    def cross_check(
        self, markets: list[CryptoMarket], listings: dict[str, dict]
    ) -> list[str]:
        """Fill missing changes from CMC and return human-readable discrepancies."""
        notes: list[str] = []
        for market in markets:
            row = listings.get(market.symbol)
            if not row:
                continue
            quote = ((row.get("quote") or {}).get("USD") or {})
            mcap = _num(quote.get("market_cap"))
            volume = _num(quote.get("volume_24h"))
            change_7d = _num(quote.get("percent_change_7d"))
            change_30d = _num(quote.get("percent_change_30d"))

            if market.change_7d is None and change_7d is not None:
                market.change_7d = change_7d
            if market.change_30d is None and change_30d is not None:
                market.change_30d = change_30d

            notes.extend(
                _compare("market cap", market.symbol, market.market_cap, mcap)
            )
            notes.extend(_compare("24h volume", market.symbol, market.volume_24h, volume))

        if notes:
            logger.info("CoinGecko/CMC disagreed on %d figures", len(notes))
        return notes


def _compare(label: str, symbol: str, first: float, second: float | None) -> list[str]:
    if second is None or first <= 0 or second <= 0:
        return []
    gap = abs(first - second) / max(first, second)
    if gap <= DISAGREEMENT_THRESHOLD:
        return []
    return [f"{symbol} {label}: CoinGecko {first:,.0f} vs CMC {second:,.0f}"]


def _num(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
