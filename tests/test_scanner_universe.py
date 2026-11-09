"""Universe filtering: what may be ranked at all."""
import json

from scanner.schemas import CryptoMarket, StockQuote
from scanner.universe import (
    is_excluded,
    load_tech_universe,
    select_crypto,
    select_stocks,
)


def market(symbol, *, mcap=500_000_000, volume=50_000_000, price=1.0, name="", **extra):
    return CryptoMarket(
        symbol=symbol, name=name or symbol, price=price,
        market_cap=mcap, volume_24h=volume, **extra,
    )


def test_stablecoin_and_wrapped_are_excluded_by_symbol():
    assert is_excluded("USDT")
    assert is_excluded("usdc")
    assert is_excluded("WBTC")
    assert is_excluded("STETH")
    assert not is_excluded("BTC")


def test_derivatives_are_excluded_by_name():
    assert is_excluded("XYZ", "Wrapped Bitcoin")
    assert is_excluded("ABC", "Staked Ethereum")
    assert not is_excluded("ABC", "Bitcoin")


def test_select_crypto_applies_both_floors():
    markets = [
        market("BTC", mcap=1_000_000_000_000, volume=20_000_000_000),
        market("SMALL", mcap=50_000_000),          # below mcap floor
        market("THIN", mcap=900_000_000, volume=1_000_000),  # below volume floor
        market("USDT", mcap=100_000_000_000, volume=50_000_000_000),
    ]
    kept = select_crypto(markets, min_market_cap_usd=100_000_000,
                         min_volume_usd=10_000_000)
    assert [m.symbol for m in kept] == ["BTC"]


def test_select_crypto_drops_zero_prices_and_duplicates():
    kept = select_crypto(
        [market("AAA"), market("AAA"), market("ZERO", price=0)],
        min_market_cap_usd=1, min_volume_usd=1,
    )
    assert [m.symbol for m in kept] == ["AAA"]


def test_load_tech_universe_reads_object_and_list(tmp_path):
    object_file = tmp_path / "a.json"
    object_file.write_text(json.dumps({"symbols": ["nvda", "AMD", "NVDA"]}))
    assert load_tech_universe(object_file) == ["NVDA", "AMD"]

    list_file = tmp_path / "b.json"
    list_file.write_text(json.dumps(["TSLA", "pltr"]))
    assert load_tech_universe(list_file) == ["TSLA", "PLTR"]


def test_load_tech_universe_missing_or_broken_file(tmp_path):
    assert load_tech_universe(tmp_path / "nope.json") == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert load_tech_universe(broken) == []


def test_select_stocks_keeps_watchlist_order_and_applies_volume_floor():
    quotes = {
        "AMD": StockQuote(symbol="AMD", price=100, avg_volume=5_000_000),
        "NVDA": StockQuote(symbol="NVDA", price=100, avg_volume=50_000_000),
        "THIN": StockQuote(symbol="THIN", price=10, avg_volume=100_000),
    }
    selected = select_stocks(
        quotes, min_average_volume=1_000_000, watchlist=["AMD", "NVDA"]
    )
    assert [q.symbol for q in selected] == ["AMD", "NVDA"]


def test_select_stocks_keeps_a_watchlist_quote_without_volume_data():
    quotes = {"NVDA": StockQuote(symbol="NVDA", price=100, avg_volume=None)}
    selected = select_stocks(
        quotes, min_average_volume=1_000_000, watchlist=["NVDA"]
    )
    assert [q.symbol for q in selected] == ["NVDA"]


def test_select_stocks_drops_an_unvetted_non_watchlist_mover():
    # "Top movers" are included only when the provider also gave a volume
    # figure to check; an unknown-volume extra is not worth the risk.
    quotes = {"MOON": StockQuote(symbol="MOON", price=10, avg_volume=None)}
    assert select_stocks(quotes, min_average_volume=1_000_000) == []


def test_select_stocks_ignores_zero_priced_quotes():
    quotes = {"NVDA": StockQuote(symbol="NVDA", price=0, avg_volume=9_000_000)}
    assert select_stocks(quotes, min_average_volume=1) == []
