import pytest
from pybit.exceptions import InvalidRequestError

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
        self.open_orders = []
        self.tickers = [{"symbol": "BTCUSDT", "lastPrice": self.last_price}]

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
        return self._ok({"list": self.tickers})

    def get_wallet_balance(self, **kwargs):
        self.calls.append(("get_wallet_balance", kwargs))
        return self._ok({"list": [{"coin": [{"walletBalance": self.wallet_balance}]}]})

    def get_positions(self, **kwargs):
        self.calls.append(("get_positions", kwargs))
        return self._ok({"list": self.positions})

    def get_open_orders(self, **kwargs):
        self.calls.append(("get_open_orders", kwargs))
        return self._ok({"list": self.open_orders})


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


def test_set_leverage_ignores_110043_raised_as_exception():
    """pybit raises InvalidRequestError for non-zero retCodes; 110043 must be swallowed."""
    settings = make_settings()

    class RaisingSession(FakeSession):
        def set_leverage(self, **kwargs):
            raise InvalidRequestError(
                request="POST /v5/position/set-leverage: body",
                message="leverage not modified",
                status_code="110043",
                time="00:00:00",
            )

    client = BybitClient(settings, session=RaisingSession())
    client.set_leverage("BTCUSDT", 5)  # must not raise


def test_set_leverage_raises_on_other_raised_errors():
    """A non-benign error code must still surface as BybitError."""
    settings = make_settings()

    class FailingSession(FakeSession):
        def set_leverage(self, **kwargs):
            raise InvalidRequestError(
                request="POST /v5/position/set-leverage: body",
                message="invalid parameter",
                status_code="10001",
                time="00:00:00",
            )

    client = BybitClient(settings, session=FailingSession())
    with pytest.raises(BybitError):
        client.set_leverage("BTCUSDT", 5)


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


def test_get_position_raises_on_api_error():
    """An API failure must surface, not be mistaken for 'flat'."""
    settings = make_settings()

    class FailingSession(FakeSession):
        def get_positions(self, **kwargs):
            raise Exception("connection reset")  # noqa: BLE001

    client = BybitClient(settings, session=FailingSession())
    with pytest.raises(BybitError):
        client.get_position("BTCUSDT")


def test_get_all_positions_passes_settle_coin_and_parses():
    """Bybit linear position-list needs symbol or settleCoin; all-pairs query uses USDT."""
    client = make_client()
    client.session.positions = [
        {"positionIdx": 0, "symbol": "BTCUSDT", "side": "Sell", "size": "0.5",
         "avgPrice": "70000", "stopLoss": "73000", "takeProfit": "",
         "unrealisedPnl": "12.5"},
        {"positionIdx": 0, "symbol": "ETHUSDT", "side": "Buy", "size": "1.0",
         "avgPrice": "3000", "stopLoss": "", "takeProfit": "3100",
         "unrealisedPnl": "-3.0"},
        {"symbol": "SOLUSDT", "size": "0", "positionIdx": 0},  # flat row: ignored
    ]
    positions = client.get_all_positions()
    get_pos_calls = [(n, p) for n, p in client.session.calls if n == "get_positions"]
    assert get_pos_calls == [("get_positions", {"category": "linear", "settleCoin": "USDT"})]
    assert positions == [
        {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avg_price": 70000.0,
         "sl": 73000.0, "tp": None, "unrealised_pnl": 12.5,
         "mark_price": None, "leverage": None, "pnl_percent": None},
        {"symbol": "ETHUSDT", "side": "Buy", "size": 1.0, "avg_price": 3000.0,
         "sl": None, "tp": 3100.0, "unrealised_pnl": -3.0,
         "mark_price": None, "leverage": None, "pnl_percent": None},
    ]


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


def test_compute_tp_sl_percent_overrides_env_defaults():
    """The dashboard passes tp/sl percents that beat the env defaults."""
    client = make_client(default_tp_percent=0.05, default_sl_percent=0.05)
    signal = TradeSignal(secret="s", symbol="BTCUSDT", side="buy", qty=0.001)
    tp, sl = client.compute_tp_sl(signal, 70000.0, tp_percent=0.03, sl_percent=0.02)
    assert tp == pytest.approx(72100.0)  # +3%
    assert sl == pytest.approx(68600.0)  # -2%
    # ...but an explicit alert price still wins over the percent override.
    signal2 = TradeSignal(secret="s", symbol="BTCUSDT", side="buy", qty=0.001,
                          tp=72000, sl=66000)
    assert client.compute_tp_sl(
        signal2, 70000.0, tp_percent=0.03, sl_percent=0.02
    ) == (72000, 66000)


def test_list_pairs_filters_sorts_and_caches():
    client = make_client()
    client.session.tickers = [
        {"symbol": "SOLUSDT", "lastPrice": "150"},
        {"symbol": "BTCUSDT", "lastPrice": "70000"},
        {"symbol": "ETHUSDT", "lastPrice": "3000"},
        {"symbol": "BTCUSD", "lastPrice": "90000"},  # not a USDT perp — excluded
    ]
    assert client.list_pairs(query="", limit=10) == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert client.list_pairs(query="eth", limit=10) == ["ETHUSDT"]
    session_calls = sum(1 for n, _ in client.session.calls if n == "get_tickers")
    assert session_calls == 1  # second query served from the cache


def test_pair_exists_true_for_known_instrument():
    client = make_client()
    assert client.pair_exists("BTCUSDT") is True


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


# ------------------------------------------------------------------ stop orders


def test_get_open_stop_orders_queries_and_parses():
    """All-pairs stop-order read passes settleCoin + StopOrder filter (open-orders
    endpoint needs a symbol/baseCoin/settleCoin for linear)."""
    client = make_client()
    client.session.open_orders = [
        {"symbol": "BTCUSDT", "stopOrderType": "PartialStopLoss", "triggerPrice": "83982.2"},
    ]
    rows = client.get_open_stop_orders()
    assert rows[0]["stopOrderType"] == "PartialStopLoss"
    name, params = client.session.calls[-1]
    assert name == "get_open_orders"
    assert params == {
        "category": "linear", "settleCoin": "USDT",
        "orderFilter": "StopOrder", "limit": 50,
    }


def test_get_open_stop_orders_raises_on_api_error():
    settings = make_settings()

    class FailingSession(FakeSession):
        def get_open_orders(self, **kwargs):
            raise Exception("connection reset")  # noqa: BLE001

    client = BybitClient(settings, session=FailingSession())
    with pytest.raises(BybitError):
        client.get_open_stop_orders()


def test_get_all_positions_overlays_real_sl_tp_from_partial_stop_orders():
    """Partial-mode TP/SL live as separate conditional orders (position-row fields
    are empty for perps); the merge must surface the real levels + live fields.

    Mirrors the user's live trade: a BTC short whose 4%-above SL sits as an
    Untriggered PartialStopLoss order.
    """
    client = make_client()
    client.session.positions = [
        {"positionIdx": 0, "symbol": "BTCUSDT", "side": "Sell", "size": "0.011",
         "avgPrice": "80752.2", "stopLoss": "", "takeProfit": "",
         "unrealisedPnl": "-23.4", "markPrice": "81600.5", "leverage": "5"},
    ]
    client.session.open_orders = [
        {"symbol": "BTCUSDT", "stopOrderType": "PartialStopLoss", "side": "Buy",
         "triggerPrice": "83982.2", "reduceOnly": True, "closeOnTrigger": True,
         "orderStatus": "Untriggered"},
        {"symbol": "BTCUSDT", "stopOrderType": "PartialTakeProfit", "side": "Buy",
         "triggerPrice": "77000.0", "reduceOnly": True, "closeOnTrigger": True,
         "orderStatus": "Untriggered"},
    ]
    positions = client.get_all_positions()
    pos = positions[0]
    assert pos["sl"] == 83982.2   # real SL from the open stop-order, not the empty row
    assert pos["tp"] == 77000.0
    assert pos["mark_price"] == 81600.5
    assert pos["leverage"] == 5
    margin = 80752.2 * 0.011 / 5
    assert pos["pnl_percent"] == pytest.approx(-23.4 / margin * 100)


def test_get_all_positions_full_mode_row_explicit_fields():
    """A Full-mode order row carrying explicit stopLoss/takeProfit sets both directly."""
    client = make_client()
    client.session.positions = [
        {"positionIdx": 0, "symbol": "BTCUSDT", "side": "Buy", "size": "0.5",
         "avgPrice": "70000", "stopLoss": "", "takeProfit": "", "unrealisedPnl": "0"},
    ]
    client.session.open_orders = [
        {"symbol": "BTCUSDT", "orderStatus": "New", "side": "Sell", "reduceOnly": True,
         "stopLoss": "67000", "takeProfit": "73000"},
    ]
    positions = client.get_all_positions()
    assert positions[0]["sl"] == 67000.0
    assert positions[0]["tp"] == 73000.0


def test_attach_stop_levels_price_relative_fallback_and_ignores_nonlive():
    """Rows without stopOrderType are classified by where the trigger sits vs entry;
    a filled/cancelled order within the recent window must not show as live."""
    client = make_client()
    client.session.positions = [
        {"positionIdx": 0, "symbol": "BTCUSDT", "side": "Buy", "size": "0.5",
         "avgPrice": "70000", "stopLoss": "", "takeProfit": "", "unrealisedPnl": "0"},
    ]
    client.session.open_orders = [
        {"symbol": "BTCUSDT", "orderStatus": "Filled", "side": "Sell", "reduceOnly": True,
         "triggerPrice": "66000"},  # stale — must not be shown as live SL
        {"symbol": "BTCUSDT", "orderStatus": "Untriggered", "side": "Sell", "reduceOnly": True,
         "triggerPrice": "73000"},  # above a Buy entry → take-profit
        {"symbol": "BTCUSDT", "orderStatus": "Untriggered", "side": "Sell", "reduceOnly": True,
         "triggerPrice": "66000"},  # below a Buy entry → stop-loss
    ]
    positions = client.get_all_positions()
    assert positions[0]["sl"] == 66000.0
    assert positions[0]["tp"] == 73000.0


def test_get_all_positions_stop_order_read_failure_keeps_position_row_levels():
    """If the stop-order read fails the position-row SL/TP remain the fallback —
    never a crash, never a missing column."""
    settings = make_settings()

    class NoStopOrdersSession(FakeSession):
        def get_open_orders(self, **kwargs):
            raise Exception("boom")  # noqa: BLE001

    client = BybitClient(settings, session=NoStopOrdersSession())
    client.session.positions = [
        {"positionIdx": 0, "symbol": "BTCUSDT", "side": "Buy", "size": "0.5",
         "avgPrice": "70000", "stopLoss": "67000", "takeProfit": "",
         "unrealisedPnl": "0"},
    ]
    positions = client.get_all_positions()
    assert positions[0]["sl"] == 67000.0


def test_pnl_percent_none_without_leverage():
    """pnl_percent needs the position margin (needs leverage); absent → None."""
    client = make_client()
    client.session.positions = [
        {"positionIdx": 0, "symbol": "BTCUSDT", "side": "Buy", "size": "0.5",
         "avgPrice": "70000", "unrealisedPnl": "12.5"},  # no markPrice/leverage keys
    ]
    positions = client.get_all_positions()
    assert positions[0]["leverage"] is None
    assert positions[0]["mark_price"] is None
    assert positions[0]["pnl_percent"] is None
