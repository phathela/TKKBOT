"""Deterministic market metrics.

The headline number is :func:`volatility_pct` — the 24h (crypto) or current
trading day (stocks) range divided by the low. Everything here is pure and
unit-tested; no I/O.
"""
from __future__ import annotations

from typing import Iterable, Sequence

Number = float | int | None


def _positive(value: Number) -> float | None:
    """Return ``value`` as a float strictly above zero, else ``None``."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number if number > 0 else None


def volatility_pct(high: Number, low: Number) -> float | None:
    """``((high - low) / low) * 100``.

    Returns ``None`` when either figure is missing or the low is not positive —
    a missing/zero low would otherwise divide by zero or produce nonsense.
    A high below the low (bad data) is also rejected.
    """
    high_value = _positive(high)
    low_value = _positive(low)
    if high_value is None or low_value is None:
        return None
    if high_value < low_value:
        return None
    return (high_value - low_value) / low_value * 100.0


def pct_change(new: Number, old: Number) -> float | None:
    """Percentage change from ``old`` to ``new`` (``None`` if unusable)."""
    new_value = _positive(new)
    old_value = _positive(old)
    if new_value is None or old_value is None:
        return None
    return (new_value - old_value) / old_value * 100.0


def volume_spike(volume: Number, average_volume: Number) -> float | None:
    """``volume / average_volume`` — 1.0 is a normal day."""
    current = _positive(volume)
    average = _positive(average_volume)
    if current is None or average is None:
        return None
    return current / average


def distance_from_reference(price: Number, reference: Number) -> float | None:
    """How far ``price`` sits from ``reference``, as a positive percentage."""
    price_value = _positive(price)
    reference_value = _positive(reference)
    if price_value is None or reference_value is None:
        return None
    return abs(price_value - reference_value) / reference_value * 100.0


def atr(
    highs: Sequence[Number],
    lows: Sequence[Number],
    closes: Sequence[Number],
    period: int = 14,
) -> float | None:
    """Wilder-style ATR over the last ``period`` true ranges.

    Accepts parallel high/low/close series ordered oldest-first. Needs at least
    two complete bars; returns ``None`` when it cannot be computed.
    """
    if period <= 0 or not highs or len(highs) != len(lows) or len(lows) != len(closes):
        return None
    ranges: list[float] = []
    previous_close: float | None = None
    for high, low, close in zip(highs, lows, closes):
        h, l, c = _positive(high), _positive(low), _positive(close)
        if h is None or l is None or c is None or h < l:
            previous_close = c
            continue
        candidates = [h - l]
        if previous_close is not None:
            candidates.extend([abs(h - previous_close), abs(l - previous_close)])
        ranges.append(max(candidates))
        previous_close = c
    if len(ranges) < 2:
        return None
    window = ranges[-period:]
    return sum(window) / len(window)


def atr_pct(atr_value: Number, price: Number) -> float | None:
    """ATR expressed as a percentage of price."""
    atr_number = _positive(atr_value)
    price_value = _positive(price)
    if atr_number is None or price_value is None:
        return None
    return atr_number / price_value * 100.0


def mean(values: Iterable[Number]) -> float | None:
    """Mean of the usable (positive-or-zero finite) values, else ``None``."""
    usable: list[float] = []
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != number or number in (float("inf"), float("-inf")):
            continue
        usable.append(number)
    if not usable:
        return None
    return sum(usable) / len(usable)
