"""Volatility and the supporting metrics."""
import pytest

from scanner.metrics import (
    atr,
    atr_pct,
    distance_from_reference,
    mean,
    pct_change,
    volatility_pct,
    volume_spike,
)


def test_volatility_is_range_over_low():
    assert volatility_pct(110, 100) == pytest.approx(10.0)


def test_volatility_reads_the_low_not_the_high():
    # (120-100)/100 = 20%, not (120-100)/120 = 16.7%
    assert volatility_pct(120, 100) == pytest.approx(20.0)


def test_volatility_flat_market_is_zero():
    assert volatility_pct(100, 100) == pytest.approx(0.0)


@pytest.mark.parametrize("high,low", [(None, 100), (110, None), (110, 0), (110, -5)])
def test_volatility_missing_or_zero_low_is_none(high, low):
    assert volatility_pct(high, low) is None


def test_volatility_rejects_inverted_data():
    assert volatility_pct(90, 100) is None


def test_volatility_accepts_strings_from_json():
    assert volatility_pct("110", "100") == pytest.approx(10.0)


def test_pct_change():
    assert pct_change(105, 100) == pytest.approx(5.0)
    assert pct_change(95, 100) == pytest.approx(-5.0)
    assert pct_change(None, 100) is None
    assert pct_change(10, 0) is None


def test_volume_spike():
    assert volume_spike(250, 100) == pytest.approx(2.5)
    assert volume_spike(100, 0) is None
    assert volume_spike(None, 100) is None


def test_distance_from_reference_is_absolute():
    assert distance_from_reference(95, 100) == pytest.approx(5.0)
    assert distance_from_reference(105, 100) == pytest.approx(5.0)
    assert distance_from_reference(None, 100) is None


def test_atr_averages_true_ranges():
    highs = [10, 11, 12, 13]
    lows = [9, 10, 11, 12]
    closes = [9.5, 10.5, 11.5, 12.5]
    # TRs: 1, then 1.5 each (the bars trade outside the previous close).
    assert atr(highs, lows, closes, period=4) == pytest.approx(1.375)


def test_atr_needs_two_bars_and_equal_lengths():
    assert atr([10], [9], [9.5]) is None
    assert atr([10, 11], [9], [9.5, 10]) is None
    assert atr([], [], []) is None


def test_atr_skips_incomplete_rows():
    highs = [10, None, 12, 13]
    lows = [9, 9.5, 11, 12]
    closes = [9.5, 10, 11.5, 12.5]
    assert atr(highs, lows, closes, period=3) is not None


def test_atr_pct():
    assert atr_pct(2, 100) == pytest.approx(2.0)
    assert atr_pct(None, 100) is None


def test_mean_ignores_missing_values():
    assert mean([1, None, 3]) == pytest.approx(2.0)
    assert mean([None]) is None
