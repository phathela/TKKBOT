import pytest

from conftest import make_settings
from app.bybit_client import BybitClient, BybitError, format_qty
from app.signals import TradeSignal


class FakeSession:
    """Records calls and returns canned Bybit-shaped responses."""

    def __init__(self, retcode=0, retmsg="ok"):
        self.calls = []
        self.retcode = retcode
        self.retmsg = retmsg
        self.qty_step = "0.001"
        self.last_price = "70000"
        self.wallet_balance = "1000"
        self.positions = []

    def _ok(self, result):
        return {"retCode": self.retcode, "retMsg": self.retmsg, "result": result}

    def get_instruments_info(self, **kwargs):
        self.calls.append(("get_instruments_info", kwargs))
        return self._ok({"list": [{"lotSizeFilter": {"qtyStep": self.qty_step}}]})

    def set_leverage(self, **kwargs):
        self.calls.append(("set_leverage", kwargs))
        return self._ok({})

    def place_order(self, **kwargs):
        self.calls.append(("place_order", kwargs))
        return self._ok({"orderId": "O123", "orderLinkId": kwargs.get("orderLinkId")})

    def get_tickers(self, **kwargs):
        self.calls.append(("get_tickers", kwargs))
        return self._ok({"list": [{"lastPrice": self.last_price}]})

    def get_wallet_balance(self, **kwargs):
        self.calls.append(("get_wallet_balance", kwargs))
        return self._ok({"list": [{"coin": [{"walletBalance": self.wallet_balance}]}]})

    def get_positions(self, **kwargs):
        self.calls.append(("get_positions", kwargs))
        return self._ok({"list": self.positions})


def make_client(retcode=0, **settings_overrides):
    settings = make_settings(**settings_overrides)
    return BybitClient(settings, session=FakeSession(retcode=retcode))


def test_place_market_order_builds_correct_params():
    client = make_client()
    client.place_market_order("BTCUSDT", "Buy", 0.001, leverage=5, tp=72000, sl=66000)

    name, params = client.session.calls[0]
    assert (name, params) == (
        "set_leverage",
        {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "5", "sellLeverage": "5"},
    )

    name, params = client.session.calls[-1]
    assert name == "place_order"
    assert params["category"] == "linear"
    assert params["symbol"] == "BTCUSDT"
    assert params["side"] == "Buy"
    assert params["orderType"] == "Market"
    assert params["qty"] == "0.001"
    assert params["timeInForce"] == "IOC"
    assert params["positionIdx"] == 0
    assert params["takeProfit"] == "72000"
    assert params["stopLoss"] == "66000"
    assert params["tpslMode"] == "Partial"
    assert params["orderLinkId"].startswith("tkkbot_")


def test_place_market_order_rounds_qty_to_lot_step():
    client = make_client()
    client.place_market_order("BTCUSDT", "Buy", 0.007142857)
    name, params = client.session.calls[-1]
    assert name == "place_order"
    assert params["qty"] == "0.007"


def test_place_market_order_without_tp_sl_omits_fields():
    client = make_client()
    client.place_market_order("BTCUSDT", "Buy", 0.001)
    name, params = client.session.calls[-1]
    assert name == "place_order"
    assert "takeProfit" not in params
    assert "stopLoss" not in params
    assert "tpslMode" not in params


def test_set_leverage_ignores_110043():
    client = make_client(retcode=110043)
    client.set_leverage("BTCUSDT", 5)  # must not raise


def test_retcode_error_raises():
    client = make_client(retcode=10001, retmsg="bad request")
    with pytest.raises(BybitError):
        client.place_market_order("BTCUSDT", "Buy", 0.001)


def test_close_position_reduce_only():
    client = make_client()
    client.session.positions = [
        {"positionIdx": 0, "side": "Buy", "size": "0.5", "avgPrice": "69000"}
    ]
    result = client.close_position("BTCUSDT")
    name, params = client.session.calls[-1]
    assert name == "place_order"
    assert params["side"] == "Sell"
    assert params["reduceOnly"] is True
    assert params["qty"] == "0.5"
    assert result["closed_qty"] == 0.5


def test_close_position_flat_raises():
    client = make_client()
    with pytest.raises(BybitError):
        client.close_position("BTCUSDT")


def test_close_position_qty_capped_at_position_size():
    client = make_client()
    client.session.positions = [
        {"positionIdx": 0, "side": "Sell", "size": "0.5", "avgPrice": "69000"}
    ]
    client.close_position("BTCUSDT", qty=1.0)
    name, params = client.session.calls[-1]
    assert params["side"] == "Buy"
    assert params["qty"] == "0.5"  # min(1.0, 0.5)


def test_compute_tp_sl_explicit_values_win():
    client = make_client(default_tp_percent=0.05, default_sl_percent=0.05)
    signal = TradeSignal(secret="s", symbol="BTCUSDT", side="buy", qty=0.001, tp=72000, sl=66000)
    tp, sl = client.compute_tp_sl(signal, 70000.0)
    assert tp == 72000 and sl == 66000


def test_compute_tp_sl_from_percent():
    client = make_client(default_tp_percent=0.05, default_sl_percent=0.05)
    signal = TradeSignal(secret="s", symbol="BTCUSDT", side="buy", qty=0.001)
    tp, sl = client.compute_tp_sl(signal, 70000.0)
    assert tp == pytest.approx(73500.0)
    assert sl == pytest.approx(66500.0)


def test_compute_tp_sl_sell_side_flipped():
    client = make_client(default_tp_percent=0.05, default_sl_percent=0.05)
    signal = TradeSignal(secret="s", symbol="BTCUSDT", side="sell", qty=0.001)
    tp, sl = client.compute_tp_sl(signal, 70000.0)
    assert tp == pytest.approx(66500.0)
    assert sl == pytest.approx(73500.0)


def test_format_qty():
    assert format_qty(0.001) == "0.001"
    assert format_qty(1.0) == "1"
    assert format_qty(0.00000123) == "0.00000123"
