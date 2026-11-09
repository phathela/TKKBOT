"""Deterministic pre-scoring.

All the numbers live here; the LLM only ever ranks an already-computed
shortlist. Weights come from ``scoring_weights.json`` (or
``SCORING_WEIGHTS_PATH``) so they can be tuned without a deploy.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

from . import metrics
from .schemas import Candidate, CryptoMarket, StockQuote

logger = logging.getLogger(__name__)

DEFAULT_WEIGHTS: dict[str, float] = {
    "volatility": 0.40,
    "volume_spike": 0.25,
    "momentum": 0.20,
    "liquidity": 0.15,
}


def load_weights(path: Path | None) -> dict[str, float]:
    """Load and validate the weight file, falling back to the defaults."""
    weights = dict(DEFAULT_WEIGHTS)
    if path is not None:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.info("No scoring weights at %s; using defaults", path)
            raw = None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Scoring weights unreadable (%s): %s", path, exc)
            raw = None
        if isinstance(raw, dict):
            merged = {}
            for key, default in DEFAULT_WEIGHTS.items():
                value = raw.get(key, default)
                if isinstance(value, (int, float)) and value >= 0:
                    merged[key] = float(value)
                else:
                    logger.warning("Ignoring invalid weight %s=%r", key, value)
                    merged[key] = default
            weights = merged
    total = sum(weights.values())
    if total <= 0:
        logger.warning("Scoring weights sum to zero; using the defaults")
        return dict(DEFAULT_WEIGHTS)
    return {key: value / total for key, value in weights.items()}


def percentile_scores(values: list[float | None]) -> list[float]:
    """Position of each value within the set, in ``[0, 1]``; missing scores 0.

    Ties share a score, so two identical metrics cannot be separated by luck.
    A single observation scores 0.5 (it is neither the best nor the worst).
    """
    present = sorted({v for v in values if v is not None and not _is_nan(v)})
    if not present:
        return [0.0 for _ in values]
    if len(present) == 1:
        return [0.5 if v is not None and not _is_nan(v) else 0.0 for v in values]
    index = {value: position / (len(present) - 1)
             for position, value in enumerate(present)}
    return [index[v] if v is not None and not _is_nan(v) else 0.0 for v in values]


def score_candidates(
    candidates: list[Candidate], weights: dict[str, float]
) -> list[Candidate]:
    """Attach component scores and a weighted total; return best-first."""
    if not candidates:
        return []
    volatility = percentile_scores([c.volatility_pct for c in candidates])
    spike = percentile_scores([c.volume_spike for c in candidates])
    # Momentum is a magnitude: a violent move is interesting in either direction.
    momentum = percentile_scores([
        abs(c.momentum_pct) if c.momentum_pct is not None else None
        for c in candidates
    ])
    liquidity = percentile_scores([c.liquidity_usd for c in candidates])

    for candidate, vol_score, spike_score, mom_score, liq_score in zip(
        candidates, volatility, spike, momentum, liquidity
    ):
        candidate.components = {
            "volatility": round(vol_score, 4),
            "volume_spike": round(spike_score, 4),
            "momentum": round(mom_score, 4),
            "liquidity": round(liq_score, 4),
        }
        candidate.score = round(
            weights.get("volatility", 0.0) * vol_score
            + weights.get("volume_spike", 0.0) * spike_score
            + weights.get("momentum", 0.0) * mom_score
            + weights.get("liquidity", 0.0) * liq_score,
            4,
        )

    return sorted(candidates, key=lambda c: (-c.score, c.symbol))


def build_crypto_candidates(markets: list[CryptoMarket]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for market in markets:
        vol = metrics.volatility_pct(market.high_24h, market.low_24h)
        candidates.append(
            Candidate(
                asset_type="crypto",
                symbol=market.symbol,
                name=market.name,
                price=market.price,
                volatility_pct=_round(vol),
                volume_spike=None,  # filled from candles/Bybit where available
                momentum_pct=_round(
                    market.change_7d if market.change_7d is not None
                    else market.change_24h
                ),
                liquidity_usd=market.market_cap,
                change_24h_pct=_round(market.change_24h),
                change_7d_pct=_round(market.change_7d),
                distance_from_high_pct=_round(
                    metrics.distance_from_reference(market.price, market.high_24h)
                ),
                distance_from_low_pct=_round(
                    metrics.distance_from_reference(market.price, market.low_24h)
                ),
                extra={"volume_24h": market.volume_24h},
            )
        )
    return candidates


def build_stock_candidates(quotes: list[StockQuote]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for quote in quotes:
        vol = metrics.volatility_pct(quote.high, quote.low)
        dollar_volume = (
            quote.avg_volume * quote.price
            if quote.avg_volume is not None and quote.price > 0
            else None
        )
        candidates.append(
            Candidate(
                asset_type="stock",
                symbol=quote.symbol,
                name=quote.name or quote.symbol,
                price=quote.price,
                volatility_pct=_round(vol),
                volume_spike=_round(metrics.volume_spike(quote.volume, quote.avg_volume)),
                momentum_pct=_round(quote.change_pct),
                liquidity_usd=dollar_volume,
                change_24h_pct=_round(quote.change_pct),
                distance_from_high_pct=_round(
                    metrics.distance_from_reference(quote.price, quote.high)
                ),
                distance_from_low_pct=_round(
                    metrics.distance_from_reference(quote.price, quote.low)
                ),
                extra={"stale": "yes" if quote.stale else "no"},
            )
        )
    return candidates


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return round(value, 4)


def _is_nan(value: float) -> bool:
    return isinstance(value, float) and (math.isnan(value) or math.isinf(value))
