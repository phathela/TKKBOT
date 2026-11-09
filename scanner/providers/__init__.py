"""Data providers. Each one is optional and degrades independently."""
from .base import HttpClient, Provider, ProviderError
from .bybit import BybitProvider
from .coinmarketcap import CoinMarketCapProvider
from .coingecko import CoinGeckoProvider
from .fear_greed import FearGreedProvider
from .finnhub import FinnhubProvider
from .investingcom import InvestingComProvider
from .twelvedata import TwelveDataProvider
from .yahoo import YahooProvider

__all__ = [
    "HttpClient",
    "Provider",
    "ProviderError",
    "BybitProvider",
    "CoinMarketCapProvider",
    "CoinGeckoProvider",
    "FearGreedProvider",
    "FinnhubProvider",
    "InvestingComProvider",
    "TwelveDataProvider",
    "YahooProvider",
]
