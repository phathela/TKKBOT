import json

import pytest

from conftest import make_settings
from app.runtime_config import ConfigError, RuntimeConfig


def make_runtime(tmp_path, **settings_overrides):
    settings = make_settings(**settings_overrides)
    return RuntimeConfig(settings, path=str(tmp_path / "cfg.json"))


def test_defaults_come_from_settings():
    runtime = RuntimeConfig(make_settings(), path="")
    assert runtime.leverage == 5
    assert runtime.sl_percent == pytest.approx(0.04)
    assert runtime.margin_percent == pytest.approx(0.90)
    assert runtime.trading_enabled is True
    assert runtime.allowed == {"BTCUSDT"}
    assert runtime.persistence_path is None


def test_update_returns_full_state_and_applies(tmp_path):
    runtime = make_runtime(tmp_path, max_leverage=100)
    state = runtime.update({
        "leverage": 10,
        "sl_percent": 0.03,
        "tp_percent": 0.01,
        "margin_percent": 0.5,
    })
    assert state["leverage"] == 10
    assert state["sl_percent"] == pytest.approx(0.03)
    assert state["tp_percent"] == pytest.approx(0.01)
    assert state["margin_percent"] == pytest.approx(0.5)


def test_partial_update_only_changes_given_fields(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.update({"margin_percent": 0.6})
    assert runtime.margin_percent == pytest.approx(0.6)
    assert runtime.leverage == 5  # untouched
    assert runtime.sl_percent == pytest.approx(0.04)


def test_round_trip_persists_across_reload(tmp_path):
    path = tmp_path / "cfg.json"
    runtime = RuntimeConfig(make_settings(max_leverage=100), path=str(path))
    runtime.update({"leverage": 12, "sl_percent": 0.025, "tp_percent": 0.0,
                    "trading_enabled": False, "pairs_add": ["ETHUSDT", "SOLUSDT"]})

    # Simulate a restart: a brand-new store reading the same file.
    fresh = RuntimeConfig(make_settings(max_leverage=100), path=str(path))
    assert fresh.leverage == 12
    assert fresh.sl_percent == pytest.approx(0.025)
    assert fresh.trading_enabled is False
    assert fresh.allowed == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    assert fresh.persistence_path == str(path)


def test_pair_add_and_remove(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.update({"pairs_add": ["ETHUSDT"]})
    runtime.update({"pairs_remove": ["BTCUSDT"]})
    assert runtime.allowed == {"ETHUSDT"}


def test_in_memory_no_path_does_not_write(tmp_path):
    runtime = RuntimeConfig(make_settings(max_leverage=100), path="")
    runtime.update({"leverage": 7})
    assert runtime.persistence_path is None
    assert not (tmp_path / "cfg.json").exists()


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text("{ not valid json !!!", encoding="utf-8")
    runtime = RuntimeConfig(make_settings(), path=str(path))
    assert runtime.leverage == 5
    assert runtime.sl_percent == pytest.approx(0.04)


def test_out_of_range_persisted_value_is_ignored(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"leverage": 999, "sl_percent": 9.0}), encoding="utf-8")
    runtime = RuntimeConfig(make_settings(), path=str(path))
    assert runtime.leverage == 5  # 999 > max_leverage 5 → env default kept
    assert runtime.sl_percent == pytest.approx(0.04)


@pytest.mark.parametrize("payload", [
    {"leverage": 0},
    {"leverage": 6},          # over max_leverage (5 by default)
    {"leverage": 2.5},        # must be an integer
    {"sl_percent": 0},
    {"sl_percent": 0.9},      # > 0.5
    {"tp_percent": -0.1},
    {"margin_percent": 1.1},
    {"margin_percent": 0.01},
    {"trading_enabled": "yes"},
    {"pairs_add": ["not a pair"]},
])
def test_invalid_updates_raise(payload, tmp_path):
    runtime = make_runtime(tmp_path)
    with pytest.raises(ConfigError):
        runtime.update(payload)


def test_pair_add_verifies_against_bybit_when_client_available(tmp_path):
    class VerifyingClient:
        def pair_exists(self, symbol):
            return symbol == "ETHUSDT"

    runtime = make_runtime(tmp_path)
    runtime._client = VerifyingClient()  # production wires the real client
    with pytest.raises(ConfigError):
        runtime.update({"pairs_add": ["NOPEUSDT"]})
    runtime.update({"pairs_add": ["ETHUSDT"]})
    assert "ETHUSDT" in runtime.allowed


def test_leverage_can_rise_when_ceiling_raised(tmp_path):
    runtime = make_runtime(tmp_path, max_leverage=100)
    runtime.update({"leverage": 50})
    assert runtime.leverage == 50
