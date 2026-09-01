import pytest
from fastapi.testclient import TestClient

from conftest import make_settings
from app.bybit_client import BybitError
from app.main import create_app


class MockClient:
    """Duck-typed stand-in for BybitClient that records what it is asked to do."""

    def __init__(self, settings):
        self.settings = settings
        self.orders = []
        self.price = 70000.0
        self.balance = 1000.0

    def get_price(self, symbol):
        return self.price

    def get_balance(self):
        return self.balance

    def compute_tp_sl(self, signal, price):
        tp, sl = signal.tp, signal.sl
        if tp is None and self.settings.default_tp_percent > 0:
            tp = price * (1 + self.settings.default_tp_percent) if signal.side == "buy" \
                else price * (1 - self.settings.default_tp_percent)
        if sl is None and self.settings.default_sl_percent > 0:
            sl = price * (1 - self.settings.default_sl_percent) if signal.side == "buy" \
                else price * (1 + self.settings.default_sl_percent)
        return tp, sl

    def place_market_order(self, symbol, side, qty, leverage=None, tp=None, sl=None):
        self.orders.append(
            {"symbol": symbol, "side": side, "qty": qty, "leverage": leverage, "tp": tp, "sl": sl}
        )
        return {"order_id": "O123"}

    def close_position(self, symbol, qty=None):
        self.orders.append({"symbol": symbol, "close": True, "qty": qty})
        return {"order_id": "O999", "closed_qty": 0.5}


def make_app(**overrides):
    settings = make_settings(**overrides)
    client = MockClient(settings)
    app = create_app(settings=settings, client=client)
    return TestClient(app), client


def payload(**overrides):
    data = {
        "secret": "test-secret",
        "symbol": "BTCUSDT",
        "side": "buy",
        "qty": 0.001,
        "leverage": 5,
        "tp": 72000,
        "sl": 66000,
    }
    data.update(overrides)
    return data


def test_health_and_status_routes():
    tc, _ = make_app()
    assert tc.get("/").status_code == 200
    assert tc.get("/health").status_code == 200
    status = tc.get("/status")
    assert status.status_code == 200
    assert status.json()["trading_enabled"] is True


def test_valid_buy_places_entry_order():
    tc, client = make_app()
    resp = tc.post("/webhook/tradingview", json=payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is True
    assert body["action"] == "entry"
    assert client.orders == [
        {"symbol": "BTCUSDT", "side": "Buy", "qty": 0.001, "leverage": 5, "tp": 72000, "sl": 66000}
    ]


def test_bad_secret_rejected_no_trade():
    tc, client = make_app()
    resp = tc.post("/webhook/tradingview", json=payload(secret="wrong"))
    assert resp.status_code == 401
    assert client.orders == []


def test_malformed_body_rejected():
    tc, client = make_app()
    resp = tc.post("/webhook/tradingview", content="not json")
    assert resp.status_code == 400
    assert client.orders == []


def test_disallowed_symbol_rejected():
    tc, client = make_app()
    resp = tc.post("/webhook/tradingview", json=payload(symbol="DOGEUSDT"))
    assert resp.status_code == 400
    assert client.orders == []


def test_qty_over_cap_rejected():
    tc, client = make_app()
    resp = tc.post("/webhook/tradingview", json=payload(qty=5))
    assert resp.status_code == 400
    assert client.orders == []


def test_notional_over_cap_rejected():
    tc, client = make_app()
    resp = tc.post("/webhook/tradingview", json=payload(qty=0.02))  # 0.02 * 70000 = 1400 > 1000
    assert resp.status_code == 400
    assert client.orders == []


def test_kill_switch_blocks_all_trades():
    tc, client = make_app(trading_enabled=False)
    resp = tc.post("/webhook/tradingview", json=payload())
    assert resp.status_code == 503
    assert client.orders == []


def test_unconfigured_bot_fails_closed():
    tc, client = make_app(webhook_secret="")
    resp = tc.post("/webhook/tradingview", json=payload())
    assert resp.status_code == 503
    assert client.orders == []


def test_duplicate_alert_suppressed_by_cooldown():
    tc, client = make_app()
    first = tc.post("/webhook/tradingview", json=payload())
    second = tc.post("/webhook/tradingview", json=payload())
    assert first.status_code == 200 and first.json()["accepted"] is True
    assert second.status_code == 200 and second.json()["accepted"] is False
    assert len(client.orders) == 1


def test_close_action_closes_position():
    tc, client = make_app()
    data = payload(side="close")
    data.pop("qty")  # close with no qty = close the full position
    resp = tc.post("/webhook/tradingview", json=data)
    assert resp.status_code == 200
    assert resp.json()["action"] == "close"
    assert client.orders == [{"symbol": "BTCUSDT", "close": True, "qty": None}]


def test_auto_size_qty_when_omitted():
    tc, client = make_app()
    data = payload()
    data.pop("qty")
    resp = tc.post("/webhook/tradingview", json=data)
    assert resp.status_code == 200
    # balance=1000, margin=min(1000,100)=100, lev=5, price=70000
    assert client.orders[0]["qty"] == pytest.approx((100 * 5) / 70000)


def test_bybit_error_returns_502():
    class FailingClient(MockClient):
        def place_market_order(self, symbol, side, qty, leverage=None, tp=None, sl=None):
            raise BybitError("boom")

    settings = make_settings()
    client = FailingClient(settings)
    app = create_app(settings=settings, client=client)
    tc = TestClient(app)
    resp = tc.post("/webhook/tradingview", json=payload())
    assert resp.status_code == 502
