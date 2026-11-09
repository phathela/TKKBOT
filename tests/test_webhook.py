import math
import re

import pytest
from fastapi.testclient import TestClient

from conftest import make_settings
from app.bybit_client import BybitError
from app.main import create_app
from app.runtime_config import RuntimeConfig


class MockClient:
    """Duck-typed stand-in for BybitClient that records what it is asked to do."""

    def __init__(self, settings):
        self.settings = settings
        self.orders = []
        self.price = 70000.0
        self.balance = 1000.0
        self.position = None  # {"side": "Buy"|"Sell", "size": float, "avg_price": float}
        self.qty_step = 0.0   # 0 = no rounding, like a step the exchange never sent
        self.leverage_calls = []  # [(symbol, leverage)] — set_leverage is a separate call
        self.leverage_error = None  # raise this instead, to simulate a capped symbol
        self.events = []  # timeline of every exchange call, in order

    def get_price(self, symbol):
        return self.price

    def get_balance(self):
        return self.balance

    def get_position(self, symbol):
        return self.position

    def get_qty_step(self, symbol):
        return self.qty_step

    def round_qty(self, symbol, qty):
        if self.qty_step <= 0:
            return qty
        return math.floor(qty / self.qty_step + 1e-9) * self.qty_step

    def compute_tp_sl(self, signal, price, tp_percent=None, sl_percent=None):
        tp_percent = self.settings.default_tp_percent if tp_percent is None else tp_percent
        sl_percent = self.settings.default_sl_percent if sl_percent is None else sl_percent
        tp, sl = signal.tp, signal.sl
        if tp is None and tp_percent > 0:
            tp = price * (1 + tp_percent) if signal.side == "buy" \
                else price * (1 - tp_percent)
        if sl is None and sl_percent > 0:
            sl = price * (1 - sl_percent) if signal.side == "buy" \
                else price * (1 + sl_percent)
        return tp, sl

    def set_leverage(self, symbol, leverage):
        if self.leverage_error is not None:
            raise self.leverage_error
        self.leverage_calls.append((symbol, leverage))
        self.events.append(("leverage", symbol, leverage))

    def place_market_order(self, symbol, side, qty, leverage=None, tp=None, sl=None, label=""):
        self.orders.append(
            {"symbol": symbol, "side": side, "qty": qty, "leverage": leverage, "tp": tp,
             "sl": sl, "label": label}
        )
        self.events.append(("entry", symbol, side))
        return {"order_id": "O123"}

    def close_position(self, symbol, qty=None, label=""):
        self.orders.append({"symbol": symbol, "close": True, "qty": qty, "label": label})
        self.events.append(("close", symbol))
        return {"order_id": "O999", "closed_qty": 0.5}


def make_app(runtime=None, **overrides):
    settings = make_settings(**overrides)
    client = MockClient(settings)
    app = create_app(settings=settings, client=client, runtime=runtime)
    return TestClient(app), client


def entry(side, qty, tp, sl, symbol="BTCUSDT", label="btcusdt"):
    """The order MockClient records for a directional entry.

    leverage is always None here: the webhook applies it with its own
    ``set_leverage`` call *before* the reversal close, so that a symbol whose
    maximum sits below the configured value cannot fail after the close and
    leave the bot flat. Assert on ``client.leverage_calls`` for leverage.
    """
    return {"symbol": symbol, "side": side, "qty": qty, "leverage": None,
            "tp": tp, "sl": sl, "label": label}


def close(symbol="BTCUSDT", qty=None, label="btcusdt"):
    """The order MockClient records for a reduce-only close."""
    return {"symbol": symbol, "close": True, "qty": qty, "label": label}


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
    assert client.orders == [entry("Buy", 0.001, 72000, 66000)]


def test_logs_never_contain_the_webhook_secret(capsys):
    """The alert body carries the secret; it must be masked in the logs.

    Railway stores this stdout verbatim and anyone with project access can read
    it, so a plaintext secret there is a live-trading credential leak. Asserted
    on the emitted output (capsys) because ``setup_logging`` rebuilds the root
    handlers with ``force=True``, which replaces the one caplog installs.
    """
    tc, _ = make_app()
    resp = tc.post("/webhook/tradingview", json=payload())
    assert resp.status_code == 200
    out = capsys.readouterr().out
    assert "test-secret" not in out
    assert "Webhook received" in out          # still logged, just masked
    assert re.search(r'"secret"\s*:\s*"\*\*\*"', out)


def test_logs_mask_the_secret_even_on_a_rejected_payload(capsys):
    """A malformed alert is still logged — with its secret masked."""
    tc, _ = make_app()
    resp = tc.post("/webhook/tradingview",
                   content='{"secret":"test-secret","symbol":"BTCUSDT","side":"sideways"}')
    assert resp.status_code == 400
    out = capsys.readouterr().out
    assert "test-secret" not in out
    assert "Bad payload" in out


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
    tc, client = make_app(max_qty_per_order=1.0)
    resp = tc.post("/webhook/tradingview", json=payload(qty=5))
    assert resp.status_code == 400
    assert client.orders == []


def test_notional_over_cap_rejected():
    tc, client = make_app(max_notional_usd=1000.0)
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
    assert client.orders == [close()]


def test_auto_size_qty_when_omitted():
    tc, client = make_app()
    data = payload()
    data.pop("qty")
    resp = tc.post("/webhook/tradingview", json=data)
    assert resp.status_code == 200
    # balance=1000, margin=90% = 900, lev=5, price=70000
    assert client.orders[0]["qty"] == pytest.approx((1000 * 0.90 * 5) / 70000)


def test_hold_when_already_long():
    tc, client = make_app()
    client.position = {"side": "Buy", "size": 0.001, "avg_price": 70000.0}
    resp = tc.post("/webhook/tradingview", json=payload(side="buy"))
    assert resp.status_code == 200
    assert resp.json()["action"] == "hold"
    assert client.orders == []


def test_buy_alert_reverses_open_short():
    tc, client = make_app()
    client.position = {"side": "Sell", "size": 0.001, "avg_price": 69000.0}
    resp = tc.post("/webhook/tradingview", json=payload(side="buy"))
    assert resp.status_code == 200
    assert resp.json()["action"] == "entry"
    # The close still comes first at execution time, but only after the entry
    # has been fully validated — see test_reversal_keeps_position_when_qty_rejected.
    assert client.orders == [close(), entry("Buy", 0.001, 72000, 66000)]


def sell_payload():
    data = payload(side="sell")
    data["tp"] = 68000
    data["sl"] = 73000
    return data


def test_sell_alert_opens_short_when_flat():
    tc, client = make_app()
    resp = tc.post("/webhook/tradingview", json=sell_payload())
    assert resp.status_code == 200
    assert resp.json()["action"] == "entry"
    assert resp.json()["side"] == "Sell"
    assert client.orders == [entry("Sell", 0.001, 68000, 73000)]


def test_sell_alert_reverses_open_long():
    tc, client = make_app()
    client.position = {"side": "Buy", "size": 0.001, "avg_price": 70000.0}
    resp = tc.post("/webhook/tradingview", json=sell_payload())
    assert resp.status_code == 200
    assert resp.json()["action"] == "entry"
    assert resp.json()["side"] == "Sell"
    assert client.orders == [close(), entry("Sell", 0.001, 68000, 73000)]


def test_wrong_side_sl_rejected():
    """A long-style SL on a sell alert must be refused, not sent to Bybit."""
    tc, client = make_app()
    data = sell_payload()
    data["sl"] = 66000  # below market = invalid for a Sell (short)
    resp = tc.post("/webhook/tradingview", json=data)
    assert resp.status_code == 400
    assert client.orders == []


def test_bybit_error_returns_502():
    class FailingClient(MockClient):
        def place_market_order(self, symbol, side, qty, leverage=None, tp=None, sl=None,
                               label=""):
            raise BybitError("boom")

    settings = make_settings()
    client = FailingClient(settings)
    app = create_app(settings=settings, client=client)
    tc = TestClient(app)
    resp = tc.post("/webhook/tradingview", json=payload())
    assert resp.status_code == 502


# ---- the dashboard (RuntimeConfig) is authoritative for bot-wide parameters ----

def test_alert_leverage_ignored_dashboard_wins():
    tc, client = make_app()
    resp = tc.post("/webhook/tradingview", json=payload(leverage=50))
    assert resp.status_code == 200
    assert resp.json()["leverage"] == 5  # runtime default, not the alert's 50
    assert client.leverage_calls == [("BTCUSDT", 5)]


def test_dashboard_tuned_leverage_and_sl_apply_to_next_entry():
    settings = make_settings(max_leverage=100)
    runtime = RuntimeConfig(settings)
    runtime.update({"leverage": 10, "sl_percent": 0.02})
    tc, client = make_app(runtime=runtime)
    data = payload(side="buy")
    data.pop("tp")
    data.pop("sl")  # let the % defaults apply
    data["leverage"] = 3  # alert leverage must be ignored
    resp = tc.post("/webhook/tradingview", json=data)
    assert resp.status_code == 200
    order = client.orders[0]
    assert client.leverage_calls == [("BTCUSDT", 10)]  # dashboard's 10, not the alert's 3
    assert order["sl"] == pytest.approx(70000 * 0.98)  # 2% below entry


def test_pair_added_to_runtime_allowlist_accepted():
    settings = make_settings()  # env allowlist is BTCUSDT only
    runtime = RuntimeConfig(settings)
    runtime.update({"pairs_add": ["ETHUSDT"]})
    tc, client = make_app(runtime=runtime)
    resp = tc.post("/webhook/tradingview", json=payload(symbol="ETHUSDT"))
    assert resp.status_code == 200
    assert client.orders[0]["symbol"] == "ETHUSDT"


def test_pair_removed_from_runtime_allowlist_rejected():
    settings = make_settings()
    runtime = RuntimeConfig(settings)
    runtime.update({"pairs_remove": ["BTCUSDT"]})
    tc, client = make_app(runtime=runtime)
    resp = tc.post("/webhook/tradingview", json=payload())
    assert resp.status_code == 400
    assert client.orders == []


def test_trading_toggle_off_blocks_webhook():
    settings = make_settings()
    runtime = RuntimeConfig(settings)
    runtime.update({"trading_enabled": False})
    tc, client = make_app(runtime=runtime)
    resp = tc.post("/webhook/tradingview", json=payload())
    assert resp.status_code == 503
    assert client.orders == []


# ---- per-pair labels, saved quantities, and reversal safety -----------------

def runtime_with(symbol="BTCUSDT", **meta):
    """A RuntimeConfig whose allowlist holds ``symbol`` and whose saved qty is set."""
    settings = make_settings()
    runtime = RuntimeConfig(settings)
    payload_ = {"pair_meta": {symbol: meta}} if meta else {}
    if symbol != "BTCUSDT":
        payload_["pairs_add"] = [symbol]
    if payload_:
        runtime.update(payload_)
    return runtime


def test_alert_qty_wins_over_the_saved_quantity():
    runtime = runtime_with(qty=0.5)
    tc, client = make_app(runtime=runtime)
    resp = tc.post("/webhook/tradingview", json=payload(qty=0.002))
    assert resp.status_code == 200
    assert resp.json()["qty_source"] == "alert"
    assert client.orders[0]["qty"] == 0.002


def test_saved_qty_used_when_the_alert_omits_one():
    """The point of the feature: a second pair cannot rely on auto-sizing."""
    runtime = runtime_with(qty=0.3)
    tc, client = make_app(runtime=runtime)
    data = payload()
    data.pop("qty")
    resp = tc.post("/webhook/tradingview", json=data)
    assert resp.status_code == 200
    assert resp.json()["qty_source"] == "pair_saved"
    assert client.orders[0]["qty"] == 0.3


def test_auto_size_still_applies_when_nothing_is_saved():
    tc, client = make_app(runtime=runtime_with())
    data = payload()
    data.pop("qty")
    resp = tc.post("/webhook/tradingview", json=data)
    assert resp.status_code == 200
    assert resp.json()["qty_source"] == "auto"
    assert client.orders[0]["qty"] == pytest.approx((1000 * 0.90 * 5) / 70000)


def test_close_ignores_the_saved_quantity():
    """A close means flat; using the saved qty would strand the remainder."""
    runtime = runtime_with(qty=0.3)
    tc, client = make_app(runtime=runtime)
    data = payload(side="close")
    data.pop("qty")
    resp = tc.post("/webhook/tradingview", json=data)
    assert resp.status_code == 200
    assert client.orders == [close(qty=None)]


def test_pair_label_reaches_the_orders_and_response():
    runtime = runtime_with(label="btc-scalp", qty=0.3)
    tc, client = make_app(runtime=runtime)
    client.position = {"side": "Sell", "size": 0.001, "avg_price": 69000.0}
    resp = tc.post("/webhook/tradingview", json=payload(side="buy"))
    assert resp.status_code == 200
    assert resp.json()["label"] == "btc-scalp"
    # Both legs of one trade carry the same label.
    assert [o["label"] for o in client.orders] == ["btc-scalp", "btc-scalp"]


def test_unlabelled_pair_falls_back_to_its_symbol():
    tc, client = make_app(runtime=runtime_with())
    resp = tc.post("/webhook/tradingview", json=payload())
    assert resp.json()["label"] == "btcusdt"
    assert client.orders[0]["label"] == "btcusdt"


@pytest.mark.parametrize("cap", ["max_qty_per_order", "max_notional_usd"])
def test_reversal_keeps_the_position_when_the_saved_qty_is_rejected(cap):
    """The crux: a reversal must not close first and then fail.

    The saved qty is what the alert will be sized from, so an over-cap saved
    quantity used to close the short and *then* fail validation — leaving the
    bot flat, which is exactly what the always-in-the-market design forbids.
    """
    overrides = {"max_qty_per_order": 1.0} if cap == "max_qty_per_order" \
        else {"max_notional_usd": 1000.0}
    runtime = runtime_with(qty=5.0 if cap == "max_qty_per_order" else 0.02)
    tc, client = make_app(runtime=runtime, **overrides)
    client.position = {"side": "Sell", "size": 0.001, "avg_price": 69000.0}
    data = payload(side="buy")
    data.pop("qty")  # force the saved qty

    resp = tc.post("/webhook/tradingview", json=data)

    assert resp.status_code == 400
    assert client.orders == [], "the reversal closed the position before failing"
    assert "saved quantity" in resp.json()["error"]


def test_reversal_keeps_the_position_when_the_alert_qty_is_rejected():
    """Same guarantee for a qty that came with the alert."""
    tc, client = make_app(max_qty_per_order=1.0)
    client.position = {"side": "Sell", "size": 0.001, "avg_price": 69000.0}
    resp = tc.post("/webhook/tradingview", json=payload(side="buy", qty=5))
    assert resp.status_code == 400
    assert client.orders == []


def test_reversal_keeps_the_position_when_leverage_is_refused():
    """set_leverage is an exchange call, and it can fail too — a symbol whose own
    maximum sits below the dashboard's setting, say. Because it now runs before
    the reversal close, that failure leaves the position open rather than
    stranding the bot flat."""
    tc, client = make_app()
    client.leverage_error = BybitError("leverage 20x exceeds the symbol's maximum")
    client.position = {"side": "Sell", "size": 0.001, "avg_price": 69000.0}
    resp = tc.post("/webhook/tradingview", json=payload(side="buy"))
    assert resp.status_code == 502
    assert client.orders == [], "the reversal closed the position before leverage failed"


def test_leverage_is_set_before_the_reversal_close():
    """Ordering, not just the end state: leverage, then close, then entry."""
    tc, client = make_app()
    client.position = {"side": "Sell", "size": 0.001, "avg_price": 69000.0}
    resp = tc.post("/webhook/tradingview", json=payload(side="buy"))
    assert resp.status_code == 200
    assert client.events == [("leverage", "BTCUSDT", 5), ("close", "BTCUSDT"),
                             ("entry", "BTCUSDT", "Buy")]
    # And it is not applied a second time by place_market_order.
    assert client.orders == [close(), entry("Buy", 0.001, 72000, 66000)]


def test_reversal_keeps_the_position_when_the_stop_is_inverted():
    tc, client = make_app()
    client.position = {"side": "Sell", "size": 0.001, "avg_price": 69000.0}
    # A long-style SL (below price) on a Buy: fine. Send a short-style one.
    resp = tc.post("/webhook/tradingview", json=payload(side="buy", tp=None, sl=73000))
    assert resp.status_code == 400
    assert client.orders == []


def test_qty_below_the_lot_step_is_rejected_before_the_close():
    """Rounding floors, so a tiny qty would reach Bybit as 0 and be refused."""
    tc, client = make_app()
    client.qty_step = 0.001
    client.position = {"side": "Sell", "size": 0.001, "avg_price": 69000.0}
    resp = tc.post("/webhook/tradingview", json=payload(side="buy", qty=0.0004))
    assert resp.status_code == 400
    assert client.orders == []


def test_qty_is_rounded_to_the_lot_step_before_validation():
    tc, client = make_app()
    client.qty_step = 0.001
    resp = tc.post("/webhook/tradingview", json=payload(qty=0.0019))
    assert resp.status_code == 200
    assert client.orders[0]["qty"] == pytest.approx(0.001)
