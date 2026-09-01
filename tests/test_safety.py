from unittest import mock

import pytest

from conftest import make_settings
from app.safety import (
    Cooldown,
    SafetyError,
    check_configuration,
    validate_leverage,
    validate_qty,
    validate_secret,
    validate_symbol,
    validate_trading_enabled,
)


def test_secret_match_passes():
    validate_secret("abc", make_settings(webhook_secret="abc"))


def test_secret_mismatch_returns_401():
    with pytest.raises(SafetyError) as exc_info:
        validate_secret("wrong", make_settings(webhook_secret="abc"))
    assert exc_info.value.status_code == 401


def test_symbol_allowed_passes():
    validate_symbol("BTCUSDT", make_settings(allowed_symbols="BTCUSDT,ETHUSDT"))


def test_symbol_not_allowed_returns_400():
    with pytest.raises(SafetyError) as exc_info:
        validate_symbol("DOGEUSDT", make_settings(allowed_symbols="BTCUSDT"))
    assert exc_info.value.status_code == 400


def test_qty_within_caps_passes():
    validate_qty(0.001, 70000.0, make_settings())


def test_qty_over_cap_rejected():
    with pytest.raises(SafetyError):
        validate_qty(5.0, 70000.0, make_settings(max_qty_per_order=1.0))


def test_notional_over_cap_rejected():
    with pytest.raises(SafetyError):
        validate_qty(0.02, 70000.0, make_settings(max_notional_usd=1000.0))  # 1400 > 1000


def test_leverage_over_cap_rejected():
    with pytest.raises(SafetyError):
        validate_leverage(10, make_settings(max_leverage=5))


def test_trading_disabled_rejected():
    with pytest.raises(SafetyError) as exc_info:
        validate_trading_enabled(make_settings(trading_enabled=False))
    assert exc_info.value.status_code == 503


def test_configuration_missing_secret():
    with pytest.raises(SafetyError) as exc_info:
        check_configuration(make_settings(webhook_secret=""))
    assert exc_info.value.status_code == 503


def test_cooldown_blocks_repeat():
    cooldown = Cooldown(seconds=60)
    with mock.patch("app.safety.time.monotonic", side_effect=[100.0, 150.0]):
        assert cooldown.check("x") is True
        assert cooldown.check("x") is False  # 50s < 60s window


def test_cooldown_expires():
    cooldown = Cooldown(seconds=60)
    with mock.patch("app.safety.time.monotonic", side_effect=[100.0, 200.0]):
        assert cooldown.check("x") is True
        assert cooldown.check("x") is True  # 100s > 60s window


def test_cooldown_zero_disabled():
    cooldown = Cooldown(seconds=0)
    assert cooldown.check("x") is True
    assert cooldown.check("x") is True
