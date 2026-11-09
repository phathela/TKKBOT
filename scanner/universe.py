"""Universe selection: what is even allowed to be ranked.

Crypto keeps market-cap leaders above a liquidity floor, minus stablecoins and
wrapped/staked derivatives (they cannot produce a directional opportunity).
Stocks come from a configurable watchlist seeded with liquid US tech names.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .schemas import CryptoMarket, StockQuote

logger = logging.getLogger(__name__)

# Tokens pegged to something else, or claims on another token. They are not
# tradable "opportunities": their volatility is the peg's, not a market view.
EXCLUDED_SYMBOLS: frozenset[str] = frozenset(
    {
        # stablecoins / synthetic dollars
        "USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "USDD", "FDUSD", "PYUSD",
        "GUSD", "FRAX", "LUSD", "USDE", "USDS", "RLUSD", "EURC", "EURS", "EURT",
        "USDB", "CRVUSD", "GHO", "MIM", "DOLA", "USTC", "USD1",
        # wrapped / bridged / staked derivatives
        "WBTC", "WETH", "STETH", "WSTETH", "RETH", "CBETH", "WEETH", "EZETH",
        "SFRXETH", "WBETH", "CBBTC", "BTCB", "TBTC", "RENBTC", "HBTC",
        "JITOSOL", "MSOL", "BNSOL", "STSOL", "SAVAX", "ANKRETH",
        # yield-bearing / receipt tokens that shadow a base asset
        "AUSDC", "CUSDC", "SUSDE", "SUSDS",
    }
)

# Names that betray a derivative regardless of ticker ("Wrapped BTC", "Staked ETH").
EXCLUDED_NAME_PATTERN = re.compile(
    r"\b(wrapped|staked|staking|bridged|restaked|liquid staking)\b", re.IGNORECASE
)


def is_excluded(symbol: str, name: str = "") -> bool:
    if symbol.strip().upper() in EXCLUDED_SYMBOLS:
        return True
    return bool(EXCLUDED_NAME_PATTERN.search(name or ""))


def select_crypto(
    markets: list[CryptoMarket],
    *,
    min_market_cap_usd: float,
    min_volume_usd: float,
) -> list[CryptoMarket]:
    """Filter the cap-ordered listing down to tradable candidates."""
    kept: list[CryptoMarket] = []
    seen: set[str] = set()
    for market in markets:
        symbol = market.symbol.strip().upper()
        if not symbol or symbol in seen:
            continue
        if is_excluded(symbol, market.name):
            continue
        if market.market_cap < min_market_cap_usd:
            continue
        if market.volume_24h < min_volume_usd:
            continue
        if market.price <= 0:
            continue
        seen.add(symbol)
        kept.append(market)
    logger.info(
        "Crypto universe: %d of %d markets pass mcap>=%s and volume>=%s",
        len(kept), len(markets), f"{min_market_cap_usd:,.0f}", f"{min_volume_usd:,.0f}",
    )
    return kept


def load_tech_universe(path: Path) -> list[str]:
    """Read the watchlist file; tolerate comments-free JSON only."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Tech universe file not found: %s", path)
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Tech universe file unreadable (%s): %s", path, exc)
        return []

    symbols = raw.get("symbols") if isinstance(raw, dict) else raw
    if not isinstance(symbols, list):
        logger.warning("Tech universe file must contain a list of tickers")
        return []
    cleaned: list[str] = []
    for item in symbols:
        symbol = str(item).strip().upper()
        if symbol and symbol not in cleaned:
            cleaned.append(symbol)
    return cleaned


def select_stocks(
    quotes: dict[str, StockQuote],
    *,
    min_average_volume: float,
    watchlist: list[str] | None = None,
) -> list[StockQuote]:
    """Keep watchlist names (in watchlist order), then any extra liquid movers.

    A quote with no average-volume figure is kept but logged: dropping it would
    silently empty the universe whenever the provider omits that field.
    """
    selected: list[StockQuote] = []
    seen: set[str] = set()

    def usable(quote: StockQuote) -> bool:
        if quote.price <= 0:
            return False
        if quote.avg_volume is None:
            logger.info("%s has no average volume; keeping it unfiltered", quote.symbol)
            return True
        return quote.avg_volume >= min_average_volume

    ordered = [s.strip().upper() for s in (watchlist or [])]
    for symbol in ordered:
        quote = quotes.get(symbol)
        if quote and usable(quote):
            selected.append(quote)
            seen.add(symbol)

    # Anything else the provider returned (e.g. top movers) with volume data.
    for symbol in sorted(quotes):
        if symbol in seen:
            continue
        quote = quotes[symbol]
        if usable(quote) and quote.avg_volume is not None:
            selected.append(quote)
            seen.add(symbol)

    logger.info("Stock universe: %d of %d quotes selected", len(selected), len(quotes))
    return selected
