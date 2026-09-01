import pytest

from conftest import make_settings
from app.signals import SignalError, build_signal, parse_payload


def test_parse_direct_json():
    data = parse_payload('{"secret":"s","symbol":"BTCUSDT","side":"buy","qty":0.001}')
    assert data["symbol"] == "BTCUSDT"


def test_parse_message_wrapped():
    data = parse_payload('{"message": "{\\"symbol\\":\\"BTCUSDT\\",\\"side\\":\\"buy\\"}"}')
    assert data["symbol"] == "BTCUSDT"


def test_parse_invalid_json_raises():
    with pytest.raises(SignalError):
        parse_payload("not json at all")


def test_parse_wrapped_invalid_json_raises():
    with pytest.raises(SignalError):
        parse_payload('{"message": "not json at all"}')


def test_build_signal_happy():
    signal = build_signal(
        {
            "secret": "s3cr3t",
            "symbol": "btcusdt",
            "side": "buy",
            "qty": "0.001",
            "leverage": 5,
            "tp": 72000,
            "sl": 66000,
        },
        make_settings(),
    )
    assert signal.symbol == "BTCUSDT"
    assert signal.side == "buy"
    assert signal.qty == 0.001
    assert signal.leverage == 5
    assert signal.tp == 72000
    assert signal.sl == 66000


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("buy", "buy"),
        ("Buy", "buy"),
        ("long", "buy"),
        ("open_long", "buy"),
        ("open-long", "buy"),
        ("sell", "sell"),
        ("short", "sell"),
        ("close", "close"),
        ("exit", "close"),
    ],
)
def test_side_aliases(raw, expected):
    signal = build_signal({"secret": "s", "symbol": "BTCUSDT", "side": raw}, make_settings())
    assert signal.side == expected


def test_action_alias():
    signal = build_signal({"secret": "s", "symbol": "BTCUSDT", "action": "sell", "qty": 1}, make_settings())
    assert signal.side == "sell"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BINANCE:BTCUSDT", "BTCUSDT"),
        ("BTCUSDT.P", "BTCUSDT"),
        ("BTCUSDT:P", "BTCUSDT"),
        ("BTCUSDT_P", "BTCUSDT"),
        ("BTCUSDT", "BTCUSDT"),
    ],
)
def test_symbol_normalization(raw, expected):
    signal = build_signal({"secret": "s", "symbol": raw, "side": "buy"}, make_settings())
    assert signal.symbol == expected


def test_tp_sl_aliases():
    signal = build_signal(
        {"secret": "s", "symbol": "BTCUSDT", "side": "buy", "takeProfit": 1, "stopLoss": 2},
        make_settings(),
    )
    assert signal.tp == 1 and signal.sl == 2


def test_source_id_captured():
    signal = build_signal(
        {"secret": "s", "symbol": "BTCUSDT", "side": "buy", "id": "abc_123"}, make_settings()
    )
    assert signal.source_id == "abc_123"


def test_missing_secret_raises():
    with pytest.raises(SignalError):
        build_signal({"symbol": "BTCUSDT", "side": "buy"}, make_settings())


def test_missing_symbol_raises():
    with pytest.raises(SignalError):
        build_signal({"secret": "s", "side": "buy"}, make_settings())


def test_invalid_side_raises():
    with pytest.raises(SignalError):
        build_signal({"secret": "s", "symbol": "BTCUSDT", "side": "banana"}, make_settings())


def test_bad_qty_raises():
    with pytest.raises(SignalError):
        build_signal({"secret": "s", "symbol": "BTCUSDT", "side": "buy", "qty": "abc"}, make_settings())


def test_negative_qty_raises():
    with pytest.raises(SignalError):
        build_signal({"secret": "s", "symbol": "BTCUSDT", "side": "buy", "qty": -1}, make_settings())
