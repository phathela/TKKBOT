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


# ---- pair_meta: labels and saved quantities ---------------------------------

def test_pair_meta_round_trips_across_reload(tmp_path):
    path = tmp_path / "cfg.json"
    runtime = RuntimeConfig(make_settings(), path=str(path))
    runtime.update({"pairs_add": ["ETHUSDT"],
                    "pair_meta": {"BTCUSDT": {"label": "btc-scalp", "qty": 0.005},
                                  "ETHUSDT": {"label": "eth-swing", "qty": 2.0}}})

    fresh = RuntimeConfig(make_settings(), path=str(path))
    assert fresh.pair_meta == {
        "BTCUSDT": {"label": "btc-scalp", "qty": 0.005},
        "ETHUSDT": {"label": "eth-swing", "qty": 2.0},
    }
    assert fresh.pair_label("BTCUSDT") == "btc-scalp"
    assert fresh.pair_qty("ETHUSDT") == pytest.approx(2.0)


def test_unlabelled_pair_falls_back_to_its_symbol(tmp_path):
    runtime = make_runtime(tmp_path)
    assert runtime.pair_label("BTCUSDT") == "btcusdt"
    assert runtime.pair_qty("BTCUSDT") is None


def test_label_is_lowercased_and_trimmed(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.update({"pair_meta": {"BTCUSDT": {"label": "  BTC-Scalp  "}}})
    assert runtime.pair_meta["BTCUSDT"]["label"] == "btc-scalp"


@pytest.mark.parametrize("label", ["-lead", "_lead", "has space",
                                   "has.dot", "a" * 21, 0, "é"])
def test_invalid_label_rejected(tmp_path, label):
    runtime = make_runtime(tmp_path)
    with pytest.raises(ConfigError):
        runtime.update({"pair_meta": {"BTCUSDT": {"label": label}}})


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_label_clears_rather_than_errors(tmp_path, blank):
    """An emptied dashboard input sends "", so it means "no label"."""
    runtime = make_runtime(tmp_path)
    runtime.update({"pair_meta": {"BTCUSDT": {"label": "btc"}}})
    runtime.update({"pair_meta": {"BTCUSDT": {"label": blank}}})
    assert runtime.pair_label("BTCUSDT") == "btcusdt"


@pytest.mark.parametrize("qty", [0, -1, "abc", None.__class__, float("nan"),
                                 float("inf"), True])
def test_invalid_qty_rejected(tmp_path, qty):
    runtime = make_runtime(tmp_path)
    with pytest.raises(ConfigError):
        runtime.update({"pair_meta": {"BTCUSDT": {"qty": qty}}})


def test_fields_merge_so_saving_one_keeps_the_other(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.update({"pair_meta": {"BTCUSDT": {"label": "btc", "qty": 0.005}}})
    runtime.update({"pair_meta": {"BTCUSDT": {"qty": 0.01}}})
    assert runtime.pair_meta["BTCUSDT"] == {"label": "btc", "qty": 0.01}


def test_clearing_a_field_needs_no_sentinel(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.update({"pair_meta": {"BTCUSDT": {"label": "btc", "qty": 0.005}}})
    runtime.update({"pair_meta": {"BTCUSDT": {"qty": None}}})
    assert runtime.pair_meta["BTCUSDT"] == {"label": "btc"}
    assert runtime.pair_qty("BTCUSDT") is None

    runtime.update({"pair_meta": {"BTCUSDT": None}})
    assert runtime.pair_meta == {}


def test_duplicate_labels_rejected_and_nothing_is_applied(tmp_path):
    runtime = make_runtime(tmp_path, max_leverage=100)
    runtime.update({"leverage": 12})
    with pytest.raises(ConfigError):
        runtime.update({"leverage": 9, "pairs_add": ["ETHUSDT"],
                        "pair_meta": {"BTCUSDT": {"label": "same"},
                                      "ETHUSDT": {"label": "same"}}})
    # The update is atomic: a rejected save leaves the previous state intact.
    assert runtime.pair_meta == {}
    assert runtime.leverage == 12
    assert runtime.allowed == {"BTCUSDT"}


def test_label_colliding_with_another_pairs_symbol_is_rejected(tmp_path):
    """ETHUSDT cannot be called "btcusdt" while BTCUSDT keeps its implicit name."""
    runtime = make_runtime(tmp_path)
    with pytest.raises(ConfigError):
        runtime.update({"pairs_add": ["ETHUSDT"],
                        "pair_meta": {"ETHUSDT": {"label": "btcusdt"}}})


def test_meta_unknown_field_rejected(tmp_path):
    runtime = make_runtime(tmp_path)
    with pytest.raises(ConfigError):
        runtime.update({"pair_meta": {"BTCUSDT": {"quantity": 1}}})


def test_meta_for_a_disallowed_symbol_rejected(tmp_path):
    runtime = make_runtime(tmp_path)
    with pytest.raises(ConfigError):
        runtime.update({"pair_meta": {"ETHUSDT": {"label": "nope"}}})


def test_removing_a_pair_drops_its_meta(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.update({"pairs_add": ["ETHUSDT"],
                    "pair_meta": {"BTCUSDT": {"label": "btc"}, "ETHUSDT": {"label": "eth"}}})
    state = runtime.update({"pairs_remove": ["ETHUSDT"]})
    assert "ETHUSDT" not in state["pair_meta"]
    assert state["pair_meta"] == {"BTCUSDT": {"label": "btc"}}


def test_adding_and_naming_a_pair_in_one_request(tmp_path):
    runtime = make_runtime(tmp_path)
    state = runtime.update({"pairs_add": ["ETHUSDT"],
                            "pair_meta": {"ETHUSDT": {"label": "eth", "qty": 1.5}}})
    assert state["allowed_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert runtime.pair_label("ETHUSDT") == "eth"


def test_state_exposes_pair_meta_for_the_dashboard(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.update({"pair_meta": {"BTCUSDT": {"label": "btc", "qty": 0.005}}})
    assert runtime.to_dict()["pair_meta"] == {"BTCUSDT": {"label": "btc", "qty": 0.005}}


def test_config_without_pair_meta_behaves_as_before(tmp_path):
    """An old file must load unchanged, and gain an empty pair_meta."""
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"leverage": 12, "allowed_symbols": ["BTCUSDT"]}))
    runtime = RuntimeConfig(make_settings(max_leverage=100), path=str(path))
    assert runtime.leverage == 12
    assert runtime.pair_meta == {}
    assert runtime.pair_label("BTCUSDT") == "btcusdt"


# ---- a malformed pair_meta must never discard the rest of the file ----------

@pytest.mark.parametrize("meta", [
    ["not", "an", "object"],
    "a string",
    42,
    {"BTCUSDT": "not an object"},
    {"BTCUSDT": {"label": "has space"}},
    {"BTCUSDT": {"qty": -5}},
    {"BTCUSDT": {"qty": "abc"}},
    {"BTCUSDT": {"label": 12345}},
    {"ETHUSDT": {"label": "ok"}},          # not in the allowlist
    {"BTCUSDT": {"label": "ok"}, "BTCUSDTX": {"nope": 1}},
])
def test_bad_pair_meta_is_skipped_without_losing_other_settings(tmp_path, meta):
    """_load's overlays are all-or-nothing, so meta is parsed outside that block.

    A typo in a label must not also reset the leverage and stop-loss the user
    set from the dashboard.
    """
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({
        "leverage": 12,
        "sl_percent": 0.025,
        "allowed_symbols": ["BTCUSDT"],
        "pair_meta": meta,
    }))
    runtime = RuntimeConfig(make_settings(max_leverage=100), path=str(path))
    assert runtime.leverage == 12
    assert runtime.sl_percent == pytest.approx(0.025)
    assert runtime.allowed == {"BTCUSDT"}


def test_duplicate_labels_in_a_hand_edited_file_are_dropped_not_fatal(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({
        "allowed_symbols": ["BTCUSDT", "ETHUSDT"],
        "pair_meta": {"BTCUSDT": {"label": "same"}, "ETHUSDT": {"label": "same"}},
    }))
    runtime = RuntimeConfig(make_settings(), path=str(path))
    labels = {runtime.pair_label(s) for s in runtime.allowed}
    assert len(labels) == 2, f"labels still collide: {labels}"
    assert len(runtime.pair_meta) >= 1
