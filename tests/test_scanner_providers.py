"""Providers against mocked HTTP: retries, caching, pacing and normalisation."""
import json

import httpx
import pytest

from scanner.providers.base import HttpClient, ProviderError
from scanner.providers.bybit import BybitProvider
from scanner.providers.coingecko import CoinGeckoProvider
from scanner.providers.twelvedata import TwelveDataProvider


def client_with(handler, **kwargs):
    """An HttpClient wired to a MockTransport, with sleep captured."""
    slept: list[float] = []
    http = HttpClient(
        transport=httpx.MockTransport(handler),
        sleep=slept.append,
        **kwargs,
    )
    return http, slept


# --- HttpClient ------------------------------------------------------------


def test_get_is_cached_within_a_run():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    http, _ = client_with(handler)
    assert http.get_json("https://example.test/x", params={"b": 2, "a": 1}) == {
        "ok": True
    }
    http.get_json("https://example.test/x", params={"a": 1, "b": 2})
    assert len(calls) == 1


def test_different_params_are_a_different_cache_entry():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"n": len(calls)})

    http, _ = client_with(handler)
    http.get_json("https://example.test/x", params={"page": 1})
    http.get_json("https://example.test/x", params={"page": 2})
    assert len(calls) == 2


def test_post_is_never_cached():
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"ok": True})

    http, _ = client_with(handler)
    http.post_json("https://example.test/x", {"a": 1})
    http.post_json("https://example.test/x", {"a": 1})
    assert calls == ["POST", "POST"]


def test_5xx_is_retried_with_backoff():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    http, slept = client_with(handler, retries=2, backoff_seconds=0.5)
    assert http.get_json("https://example.test/x") == {"ok": True}
    assert len(calls) == 3
    assert slept == [0.5, 1.0]


def test_retries_exhausted_raises_provider_error():
    def handler(request):
        return httpx.Response(500)

    http, _ = client_with(handler, retries=1, backoff_seconds=0)
    with pytest.raises(ProviderError, match="HTTP 500"):
        http.get_json("https://example.test/x")


def test_429_honours_retry_after():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"ok": True})

    http, slept = client_with(handler, retries=2)
    assert http.get_json("https://example.test/x") == {"ok": True}
    assert slept == [3.0]


def test_429_retry_after_is_capped():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "600"})
        return httpx.Response(200, json={"ok": True})

    http, slept = client_with(handler, retries=1)
    http.get_json("https://example.test/x")
    assert slept == [15.0]


def test_429_without_header_uses_backoff():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    http, slept = client_with(handler, retries=1, backoff_seconds=0.5)
    http.get_json("https://example.test/x")
    assert slept == [0.5]


def test_4xx_fails_fast_without_retry():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    http, slept = client_with(handler, retries=2)
    with pytest.raises(ProviderError, match="404"):
        http.get_json("https://example.test/x")
    assert len(calls) == 1
    assert slept == []


def test_non_json_body_raises():
    def handler(request):
        return httpx.Response(200, text="<html>nope</html>")

    http, _ = client_with(handler)
    with pytest.raises(ProviderError, match="non-JSON"):
        http.get_json("https://example.test/x")


def test_network_error_is_retried_then_reported():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("no route")

    http, _ = client_with(handler, retries=1, backoff_seconds=0)
    with pytest.raises(ProviderError, match="no route"):
        http.get_json("https://example.test/x")
    assert len(calls) == 2


# --- CoinGecko -------------------------------------------------------------


def test_coingecko_normalises_rows_and_sends_the_key():
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("x-cg-demo-api-key")
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[
            {
                "symbol": "btc", "name": "Bitcoin", "current_price": 60000,
                "high_24h": 62000, "low_24h": 58000, "total_volume": 1e9,
                "market_cap": 1.2e12, "price_change_percentage_24h": 2.5,
                "price_change_percentage_7d_in_currency": 7.5,
                "price_change_percentage_30d_in_currency": 15.0,
            },
            {"symbol": "", "name": "broken"},
        ])

    http, _ = client_with(handler)
    markets = CoinGeckoProvider(http, "cg-key").markets(limit=250)
    assert seen["key"] == "cg-key"
    assert seen["params"]["price_change_percentage"] == "24h,7d,30d"
    assert len(markets) == 1
    market = markets[0]
    assert market.symbol == "BTC"
    assert market.high_24h == 62_000
    assert market.change_7d == 7.5
    assert market.source == "coingecko"


def test_coingecko_rejects_a_non_list_payload():
    def handler(request):
        return httpx.Response(200, json={"error": "rate limit"})

    http, _ = client_with(handler)
    with pytest.raises(ProviderError, match="unexpected payload"):
        CoinGeckoProvider(http, "k").markets()


# --- Bybit -----------------------------------------------------------------


def test_bybit_tickers_are_keyed_by_upper_symbol():
    def handler(request):
        return httpx.Response(200, json={"result": {"list": [
            {"symbol": "BTCUSDT", "lastPrice": "60000"},
            {"symbol": "ethusdt", "lastPrice": "3000"},
            {"nope": True},
        ]}})

    http, _ = client_with(handler)
    tickers = BybitProvider(http).tickers()
    assert set(tickers) == {"BTCUSDT", "ETHUSDT"}


def test_bybit_klines_are_oldest_first():
    def handler(request):
        return httpx.Response(200, json={"result": {"list": [
            ["3", "3", "3", "3", "3", "0", "30"],
            ["2", "2", "2", "2", "2", "0", "20"],
            ["1", "1", "1", "1", "1", "0", "10"],
        ]}})

    http, _ = client_with(handler)
    candles = BybitProvider(http).klines("BTCUSDT")
    assert [row[0] for row in candles] == ["1", "2", "3"]


def test_bybit_depth_sums_both_sides():
    def handler(request):
        return httpx.Response(200, json={"result": {
            "b": [["100", "2"]],   # 200
            "a": [["101", "1"]],   # 101
        }})

    http, _ = client_with(handler)
    assert BybitProvider(http).depth_usd("BTCUSDT") == pytest.approx(301.0)


def test_bybit_depth_skips_malformed_levels():
    def handler(request):
        return httpx.Response(200, json={"result": {
            "b": [["100", "1"], ["bad"], []],
            "a": [],
        }})

    http, _ = client_with(handler)
    assert BybitProvider(http).depth_usd("BTCUSDT") == pytest.approx(100.0)


def test_bybit_depth_empty_book_is_none():
    def handler(request):
        return httpx.Response(200, json={"result": {}})

    http, _ = client_with(handler)
    assert BybitProvider(http).depth_usd("BTCUSDT") is None


# --- Twelve Data -----------------------------------------------------------


def test_twelvedata_batches_and_paces_chunks():
    requests = []

    def handler(request):
        params = dict(request.url.params)
        symbols = params["symbol"].split(",")
        requests.append(params)
        rows = {
            symbol: {
                "close": "100", "high": "110", "low": "90", "volume": "5000",
                "average_volume": "1000", "percent_change": "2.5",
                "is_market_open": True, "name": symbol,
            }
            for symbol in symbols
        }
        # The real API returns a bare quote object for one symbol and a
        # symbol-keyed mapping for several.
        return httpx.Response(200, json=rows[symbols[0]] if len(symbols) == 1 else rows)

    http, _ = client_with(handler)
    slept: list[float] = []
    provider = TwelveDataProvider(
        http, "td-key", chunk_size=8, chunk_delay_seconds=60,
        sleep=slept.append,
    )
    symbols = [f"S{i}" for i in range(9)]
    quotes = provider.quotes(symbols)

    assert len(requests) == 2
    assert len(requests[0]["symbol"].split(",")) == 8
    assert requests[0]["apikey"] == "td-key"
    assert slept == [60]
    assert len(quotes) == 9
    assert quotes["S0"].avg_volume == 1000
    assert quotes["S0"].stale is False


def test_twelvedata_single_symbol_response_is_a_bare_object():
    def handler(request):
        return httpx.Response(200, json={
            "name": "NVIDIA", "close": "120", "high": "125", "low": "118",
            "volume": "1000", "average_volume": "900", "percent_change": "1.2",
            "is_market_open": False,
        })

    http, _ = client_with(handler)
    quotes = TwelveDataProvider(http, "k").quotes(["NVDA"])
    assert quotes["NVDA"].price == 120
    assert quotes["NVDA"].stale is True


def test_twelvedata_skips_a_per_symbol_error_row():
    def handler(request):
        return httpx.Response(200, json={
            "NVDA": {"close": "120", "high": "125", "low": "118"},
            "BAD": {"code": 400, "message": "symbol not found"},
        })

    http, _ = client_with(handler)
    quotes = TwelveDataProvider(http, "k").quotes(["NVDA", "BAD"])
    assert set(quotes) == {"NVDA"}


def test_twelvedata_whole_request_error_raises():
    def handler(request):
        return httpx.Response(200, json={"code": 401, "message": "bad api key"})

    http, _ = client_with(handler)
    with pytest.raises(ProviderError, match="bad api key"):
        TwelveDataProvider(http, "k").quotes(["NVDA", "AMD"])


def test_twelvedata_chunk_failure_degrades_to_no_quotes():
    def handler(request):
        return httpx.Response(500)

    http, _ = client_with(handler, retries=0)
    assert TwelveDataProvider(http, "k").quotes(["NVDA"]) == {}
