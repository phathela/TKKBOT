"""LLM output validation, the corrective retry, and the deterministic fallback."""
import json

import pytest

from scanner import llm
from scanner.providers.base import ProviderError
from scanner.schemas import Candidate


def candidate(asset_type="crypto", symbol="AAA", price=100.0, **overrides):
    base = {
        "asset_type": asset_type,
        "symbol": symbol,
        "name": symbol,
        "price": price,
        "volatility_pct": 10.0,
        "momentum_pct": 5.0,
        "volume_spike": 1.0,
        "liquidity_usd": 1_000_000,
        "score": 0.5,
    }
    base.update(overrides)
    return Candidate(**base)


SHORTLIST = [
    candidate("crypto", "BTC", 60_000, volatility_pct=12.0),
    candidate("crypto", "ETH", 3_000, volatility_pct=8.0),
    candidate("stock", "NVDA", 120, volatility_pct=6.0),
    candidate("stock", "AMD", 150, volatility_pct=9.0),
]


def item(rank, asset_type, symbol, price, volatility=10.0, **overrides):
    base = {
        "rank": rank,
        "asset_type": asset_type,
        "symbol": symbol,
        "price": price,
        "volatility_pct": volatility,
        "direction_bias": "bullish",
        "confidence": 70,
        "thesis": "A short thesis.",
        "key_risks": ["risk"],
    }
    base.update(overrides)
    return base


def response(*items):
    return {"items": list(items)}


class FakeClient:
    """Stands in for DeepSeekClient: returns scripted replies, records calls."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def chat(self, messages):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def analyse(client, n_crypto=1, n_stocks=1, shortlist=None):
    return llm.analyse(
        shortlist=shortlist or SHORTLIST,
        headlines=[],
        client=client,
        n_crypto=n_crypto,
        n_stocks=n_stocks,
        market_open=True,
        fear_greed=None,
    )


VALID = json.dumps(response(
    item(1, "crypto", "BTC", 60_000, 12.0),
    item(2, "stock", "NVDA", 120, 6.0),
))


# --- validation ------------------------------------------------------------


def test_valid_selection_is_accepted_and_sorted():
    items = llm.validate_selection(
        response(item(2, "stock", "NVDA", 120, 6.0),
                 item(1, "crypto", "BTC", 60_000, 12.0)),
        SHORTLIST, 1, 1,
    )
    assert [i.symbol for i in items] == ["BTC", "NVDA"]


def test_symbol_outside_the_shortlist_is_rejected():
    with pytest.raises(llm.SelectionError, match="not in the candidate list"):
        llm.validate_selection(
            response(item(1, "crypto", "DOGE", 1.0),
                     item(2, "stock", "NVDA", 120, 6.0)),
            SHORTLIST, 1, 1,
        )


def test_asset_type_must_match_too():
    # ETH exists, but not as a stock, and BTC covers the crypto slot.
    with pytest.raises(llm.SelectionError, match="not in the candidate list"):
        llm.validate_selection(
            response(item(1, "stock", "ETH", 3_000, 8.0),
                     item(2, "crypto", "BTC", 60_000, 12.0)),
            SHORTLIST, 1, 1,
        )


def test_wrong_item_count_is_rejected():
    with pytest.raises(llm.SelectionError, match="expected 2 items"):
        llm.validate_selection(
            response(item(1, "crypto", "BTC", 60_000, 12.0)), SHORTLIST, 1, 1
        )


def test_wrong_crypto_stock_split_is_rejected():
    with pytest.raises(llm.SelectionError, match="expected 1 crypto and 1 stocks"):
        llm.validate_selection(
            response(item(1, "crypto", "BTC", 60_000, 12.0),
                     item(2, "crypto", "ETH", 3_000, 8.0)),
            SHORTLIST, 1, 1,
        )


def test_duplicate_ranks_are_rejected():
    with pytest.raises(llm.SelectionError, match="invalid item"):
        llm.validate_selection(
            response(item(1, "crypto", "BTC", 60_000, 12.0),
                     item(1, "stock", "NVDA", 120, 6.0)),
            SHORTLIST, 1, 1,
        )


def test_volatility_contradicting_the_data_is_rejected():
    with pytest.raises(llm.SelectionError, match="volatility_pct"):
        llm.validate_selection(
            response(item(1, "crypto", "BTC", 60_000, 30.0),
                     item(2, "stock", "NVDA", 120, 6.0)),
            SHORTLIST, 1, 1,
        )


def test_small_volatility_rounding_is_tolerated():
    # 12.0 computed, 12.5 reported: inside the 2pp absolute tolerance.
    items = llm.validate_selection(
        response(item(1, "crypto", "BTC", 60_000, 12.5),
                 item(2, "stock", "NVDA", 120, 6.0)),
        SHORTLIST, 1, 1,
    )
    assert len(items) == 2


def test_price_contradicting_the_data_is_rejected():
    with pytest.raises(llm.SelectionError, match="price"):
        llm.validate_selection(
            response(item(1, "crypto", "BTC", 90_000, 12.0),
                     item(2, "stock", "NVDA", 120, 6.0)),
            SHORTLIST, 1, 1,
        )


def test_small_price_gap_is_tolerated():
    items = llm.validate_selection(
        response(item(1, "crypto", "BTC", 63_000, 12.0),
                 item(2, "stock", "NVDA", 125, 6.0)),
        SHORTLIST, 1, 1,
    )
    assert len(items) == 2


def test_missing_items_list_is_rejected():
    with pytest.raises(llm.SelectionError, match="missing an 'items' list"):
        llm.validate_selection({"nope": []}, SHORTLIST, 1, 1)


def test_opportunities_alias_is_tolerated():
    items = llm.validate_selection(
        {"opportunities": [
            item(1, "crypto", "BTC", 60_000, 12.0),
            item(2, "stock", "NVDA", 120, 6.0),
        ]},
        SHORTLIST, 1, 1,
    )
    assert len(items) == 2


# --- analyse: retry and fallback ------------------------------------------


def test_good_response_is_used_without_retry():
    client = FakeClient(VALID)
    analysis = analyse(client)
    assert analysis.fallback is False
    assert [i.symbol for i in analysis.items] == ["BTC", "NVDA"]
    assert len(client.calls) == 1


def test_bad_json_triggers_one_corrective_retry():
    client = FakeClient("not json at all", VALID)
    analysis = analyse(client)
    assert analysis.fallback is False
    assert len(client.calls) == 2
    retry = client.calls[1]
    assert retry[-1]["role"] == "user"
    assert "rejected" in retry[-1]["content"]
    assert retry[-2]["content"] == "not json at all"


def test_two_bad_replies_fall_back_to_deterministic_items():
    client = FakeClient("nope", json.dumps(response(
        item(1, "crypto", "DOGE", 1.0),
        item(2, "stock", "NVDA", 120, 6.0),
    )))
    analysis = analyse(client)
    assert analysis.fallback is True
    assert analysis.error
    assert len(client.calls) == 2
    assert len(analysis.items) == 2
    assert {i.symbol for i in analysis.items} == {"BTC", "NVDA"}


def test_provider_error_falls_back():
    client = FakeClient(ProviderError("boom"), ProviderError("boom"))
    analysis = analyse(client)
    assert analysis.fallback is True
    assert "DeepSeek request failed" in analysis.error


def test_fallback_items_are_well_formed():
    analysis = analyse(FakeClient("x", "y"))
    for rank, item in enumerate(analysis.items, start=1):
        assert item.rank == rank
        assert 0 <= item.confidence <= 100
        assert item.thesis
        assert item.direction_bias in {"bullish", "bearish", "neutral"}
        assert item.suggested_stop is not None
        assert item.suggested_target is not None


def test_deterministic_items_respects_the_requested_split():
    shortlist = SHORTLIST + [candidate("crypto", "SOL", 150)]
    items = llm.deterministic_items(shortlist, n_crypto=2, n_stocks=1)
    assert len(items) == 3
    assert sum(1 for i in items if i.asset_type == "crypto") == 2
    assert sum(1 for i in items if i.asset_type == "stock") == 1


def test_deterministic_items_tolerates_a_missing_side():
    crypto_only = [c for c in SHORTLIST if c.asset_type == "crypto"]
    items = llm.deterministic_items(crypto_only, n_crypto=2, n_stocks=5)
    assert len(items) == 2
    assert all(i.asset_type == "crypto" for i in items)


def test_bearish_fallback_stop_is_above_price():
    shortlist = [candidate("crypto", "BTC", 100.0, momentum_pct=-10.0)]
    items = llm.deterministic_items(shortlist, 1, 0)
    assert items[0].direction_bias == "bearish"
    assert items[0].suggested_stop > 100.0
    assert items[0].suggested_target < 100.0
