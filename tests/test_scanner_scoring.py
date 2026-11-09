"""Deterministic pre-scoring: percentiles, weights and candidate building."""
import json

import pytest

from scanner import scoring
from scanner.schemas import Candidate, CryptoMarket, StockQuote


def candidate(**overrides):
    base = {
        "asset_type": "crypto",
        "symbol": "AAA",
        "price": 100.0,
        "volatility_pct": 10.0,
        "volume_spike": 1.0,
        "momentum_pct": 5.0,
        "liquidity_usd": 1_000_000_000,
    }
    base.update(overrides)
    return Candidate(**base)


# --- weights ---------------------------------------------------------------


def test_default_weights_sum_to_one():
    weights = scoring.load_weights(None)
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["volatility"] == pytest.approx(0.40)


def test_weights_file_is_normalised(tmp_path):
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"volatility": 2, "volume_spike": 1,
                                "momentum": 1, "liquidity": 0}))
    weights = scoring.load_weights(path)
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["volatility"] == pytest.approx(0.5)
    assert weights["liquidity"] == 0.0


def test_invalid_weight_falls_back_to_that_default(tmp_path):
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"volatility": "lots"}))
    weights = scoring.load_weights(path)
    assert weights["volatility"] == pytest.approx(0.40)
    assert sum(weights.values()) == pytest.approx(1.0)


def test_missing_weight_key_uses_its_default(tmp_path):
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"volatility": 1}))
    weights = scoring.load_weights(path)
    # 1.0 + the three defaults (0.25 + 0.20 + 0.15) = 1.6, then normalised.
    assert weights["volatility"] == pytest.approx(0.625)
    assert weights["volume_spike"] == pytest.approx(0.25 / 1.6)
    assert sum(weights.values()) == pytest.approx(1.0)


def test_unreadable_file_uses_defaults(tmp_path):
    path = tmp_path / "w.json"
    path.write_text("{not json")
    assert scoring.load_weights(path) == pytest.approx(scoring.load_weights(None))


def test_all_zero_weights_use_defaults(tmp_path):
    path = tmp_path / "w.json"
    path.write_text(json.dumps(
        {"volatility": 0, "volume_spike": 0, "momentum": 0, "liquidity": 0}
    ))
    assert scoring.load_weights(path) == pytest.approx(scoring.load_weights(None))


# --- percentiles -----------------------------------------------------------


def test_percentile_scores_spread_zero_to_one():
    assert scoring.percentile_scores([1, 2, 3]) == [0.0, 0.5, 1.0]


def test_percentile_scores_are_order_independent():
    assert scoring.percentile_scores([3, 1, 2]) == [1.0, 0.0, 0.5]


def test_percentile_ties_share_a_score():
    assert scoring.percentile_scores([5, 5, 9]) == [0.0, 0.0, 1.0]


def test_percentile_missing_scores_zero():
    assert scoring.percentile_scores([1, None, 3]) == [0.0, 0.0, 1.0]


def test_percentile_single_value():
    assert scoring.percentile_scores([7]) == [0.5]
    assert scoring.percentile_scores([7, None]) == [0.5, 0.0]


def test_percentile_all_missing():
    assert scoring.percentile_scores([None, None]) == [0.0, 0.0]
    assert scoring.percentile_scores([]) == []


def test_percentile_ignores_nan():
    assert scoring.percentile_scores([float("nan"), 1, 3]) == [0.0, 0.0, 1.0]


# --- scoring ---------------------------------------------------------------


def test_score_candidates_sorts_best_first():
    quiet = candidate(symbol="QUIET", volatility_pct=1, volume_spike=1,
                      momentum_pct=0, liquidity_usd=1)
    wild = candidate(symbol="WILD", volatility_pct=20, volume_spike=5,
                     momentum_pct=30, liquidity_usd=10)
    ranked = scoring.score_candidates([quiet, wild], scoring.load_weights(None))
    assert [c.symbol for c in ranked] == ["WILD", "QUIET"]
    assert ranked[0].score > ranked[1].score


def test_weights_decide_the_winner():
    volatile = candidate(symbol="VOL", volatility_pct=30, momentum_pct=0)
    moving = candidate(symbol="MOM", volatility_pct=1, momentum_pct=40)
    weights = {"volatility": 1.0, "volume_spike": 0.0, "momentum": 0.0,
               "liquidity": 0.0}
    assert [c.symbol for c in scoring.score_candidates(
        [volatile, moving], weights
    )] == ["VOL", "MOM"]


def test_score_is_the_weighted_sum_of_components():
    a = candidate(symbol="A", volatility_pct=10, volume_spike=1,
                  momentum_pct=2, liquidity_usd=100)
    b = candidate(symbol="B", volatility_pct=20, volume_spike=3,
                  momentum_pct=8, liquidity_usd=200)
    weights = {"volatility": 0.4, "volume_spike": 0.25,
               "momentum": 0.2, "liquidity": 0.15}
    scored = scoring.score_candidates([a, b], weights)
    for item in scored:
        expected = sum(weights[key] * item.components[key]
                       for key in weights)
        assert item.score == pytest.approx(expected, abs=1e-3)


def test_momentum_magnitude_favours_big_down_moves():
    down = candidate(symbol="DOWN", volatility_pct=1, momentum_pct=-30)
    flat = candidate(symbol="FLAT", volatility_pct=1, momentum_pct=0)
    weights = {"volatility": 0, "volume_spike": 0, "momentum": 1, "liquidity": 0}
    assert scoring.score_candidates([down, flat], weights)[0].symbol == "DOWN"


def test_score_candidates_empty():
    assert scoring.score_candidates([], scoring.load_weights(None)) == []


# --- candidate construction ------------------------------------------------


def test_build_crypto_candidates_computes_metrics():
    market = CryptoMarket(
        symbol="BTC", name="Bitcoin", price=110, high_24h=120, low_24h=100,
        volume_24h=5_000, market_cap=1_000_000, change_24h=2.0, change_7d=8.0,
    )
    built = scoring.build_crypto_candidates([market])[0]
    assert built.volatility_pct == pytest.approx(20.0)
    assert built.momentum_pct == pytest.approx(8.0)  # 7d preferred
    assert built.volume_spike is None  # filled by Bybit candles
    assert built.distance_from_high_pct == pytest.approx(
        abs(110 - 120) / 120 * 100, abs=1e-3
    )
    assert built.extra["volume_24h"] == 5_000


def test_build_crypto_candidates_falls_back_to_24h_momentum():
    market = CryptoMarket(symbol="ETH", price=10, high_24h=11, low_24h=9,
                          change_24h=3.5, change_7d=None)
    assert scoring.build_crypto_candidates([market])[0].momentum_pct == 3.5


def test_build_crypto_candidates_missing_range_is_none():
    market = CryptoMarket(symbol="ETH", price=10)
    assert scoring.build_crypto_candidates([market])[0].volatility_pct is None


def test_build_stock_candidates():
    quote = StockQuote(symbol="NVDA", name="NVIDIA", price=100, high=110,
                       low=90, volume=300, avg_volume=100, change_pct=4.0,
                       stale=True)
    built = scoring.build_stock_candidates([quote])[0]
    assert built.asset_type == "stock"
    assert built.volatility_pct == pytest.approx(22.2222, abs=1e-3)
    assert built.volume_spike == pytest.approx(3.0)
    assert built.liquidity_usd == pytest.approx(10_000)
    assert built.extra["stale"] == "yes"
