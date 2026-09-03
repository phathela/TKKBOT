import pytest
from fastapi.testclient import TestClient

from conftest import make_settings
from app.main import create_app
from app.runtime_config import RuntimeConfig


class FakeClient:
    """Bybit stand-in for dashboard reads/writes (webhook not exercised here)."""

    def __init__(self):
        self.balance = 123.45
        self.positions = []
        self.pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "1000PEPEUSDT"]
        self.closed = []

    def get_balance(self, coin="USDT"):
        return self.balance

    def get_all_positions(self):
        return list(self.positions)

    def list_pairs(self, query="", limit=50):
        q = query.strip().upper()
        return [p for p in self.pairs if q in p][:limit]

    def pair_exists(self, symbol):
        return symbol in self.pairs

    def close_position(self, symbol, qty=None):
        self.closed.append((symbol, qty))
        return {"order_id": "c1", "closed_qty": 0.001}


PASSWORD = "test-secret"


def build_app(cfg_path, client=None, **settings_overrides):
    settings = make_settings(webhook_secret=PASSWORD, max_leverage=100, **settings_overrides)
    client = client or FakeClient()
    runtime = RuntimeConfig(settings, path=str(cfg_path), client=client)
    return TestClient(create_app(settings=settings, client=client, runtime=runtime)), client


def login(tc):
    resp = tc.post("/dashboard/login", json={"password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp


def test_dashboard_page_served(tmp_path):
    tc, _ = build_app(tmp_path / "cfg.json")
    resp = tc.get("/dashboard")
    assert resp.status_code == 200
    assert "TKKBOT" in resp.text
    assert "tradingTrack" in resp.text


def test_state_requires_login(tmp_path):
    tc, _ = build_app(tmp_path / "cfg.json")
    assert tc.get("/api/dashboard/state").status_code == 401
    assert tc.get("/api/dashboard/pairs").status_code == 401
    assert tc.post("/api/dashboard/config", json={}).status_code == 401


def test_login_wrong_password_rejected(tmp_path):
    tc, _ = build_app(tmp_path / "cfg.json")
    resp = tc.post("/dashboard/login", json={"password": "nope"})
    assert resp.status_code == 401


def test_login_then_state(tmp_path):
    tc, _ = build_app(tmp_path / "cfg.json")
    login(tc)
    state = tc.get("/api/dashboard/state").json()
    assert state["leverage"] == 5
    assert state["sl_percent"] == pytest.approx(0.04)
    assert state["margin_percent"] == pytest.approx(0.90)
    assert state["allowed_symbols"] == ["BTCUSDT"]
    assert state["wallet_balance"] == 123.45
    assert state["positions"] == []
    assert state["persistence_path"] is not None


def test_state_only_lists_allowed_positions(tmp_path):
    client = FakeClient()
    client.positions = [
        {"symbol": "BTCUSDT", "side": "Sell", "size": 0.011, "avg_price": 77222.0,
         "sl": 80310.8, "tp": None, "unrealised_pnl": 0.05,
         "mark_price": 78110.0, "leverage": 5, "pnl_percent": 0.011},
        {"symbol": "SOLUSDT", "side": "Buy", "size": 10.0, "avg_price": 150.0,
         "sl": None, "tp": None, "unrealised_pnl": 0.0,
         "mark_price": 149.0, "leverage": None, "pnl_percent": None},
    ]
    tc, _ = build_app(tmp_path / "cfg.json", client=client)
    login(tc)
    positions = tc.get("/api/dashboard/state").json()["positions"]
    assert [p["symbol"] for p in positions] == ["BTCUSDT"]  # SOL not allowed yet
    # The live-detail fields added for the Status card flow through unchanged.
    assert positions[0]["sl"] == 80310.8
    assert positions[0]["mark_price"] == 78110.0
    assert positions[0]["leverage"] == 5
    assert positions[0]["pnl_percent"] == pytest.approx(0.011)


def test_config_update_via_api(tmp_path):
    tc, _ = build_app(tmp_path / "cfg.json")
    login(tc)
    resp = tc.post("/api/dashboard/config", json={"leverage": 7, "sl_percent": 0.03})
    assert resp.status_code == 200
    body = resp.json()
    assert body["leverage"] == 7
    assert body["sl_percent"] == pytest.approx(0.03)
    # And the running bot sees it too (state).
    assert tc.get("/api/dashboard/state").json()["leverage"] == 7


def test_config_bad_value_returns_400(tmp_path):
    tc, _ = build_app(tmp_path / "cfg.json")
    login(tc)
    resp = tc.post("/api/dashboard/config", json={"sl_percent": 0})
    assert resp.status_code == 400
    assert "sl_percent" in resp.json()["detail"]


def test_config_add_pair_verified_and_unknown_rejected(tmp_path):
    tc, _ = build_app(tmp_path / "cfg.json")
    login(tc)
    assert tc.post("/api/dashboard/config", json={"pairs_add": ["ETHUSDT"]}).status_code == 200
    assert tc.post("/api/dashboard/config", json={"pairs_add": ["NOTREALUSDT"]}).status_code == 400


def test_pairs_search(tmp_path):
    tc, _ = build_app(tmp_path / "cfg.json")
    login(tc)
    body = tc.get("/api/dashboard/pairs", params={"q": "eth"}).json()
    assert body["pairs"] == ["ETHUSDT"]
    body = tc.get("/api/dashboard/pairs").json()
    assert "BTCUSDT" in body["pairs"]


def test_close_position_via_api(tmp_path):
    client = FakeClient()
    client.positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": 0.001,
                         "avg_price": 70000.0, "sl": 67200.0, "tp": None,
                         "unrealised_pnl": 1.0}]
    tc, _ = build_app(tmp_path / "cfg.json", client=client)
    login(tc)
    resp = tc.post("/api/dashboard/close", json={"symbol": "BTCUSDT"})
    assert resp.status_code == 200
    assert client.closed == [("BTCUSDT", None)]
    # Not an allowed pair → rejected without touching Bybit.
    assert tc.post("/api/dashboard/close", json={"symbol": "DOGEUSDT"}).status_code == 400
    assert len(client.closed) == 1


def test_settings_persist_across_restart(tmp_path):
    cfg = tmp_path / "cfg.json"
    settings = make_settings(webhook_secret=PASSWORD, max_leverage=100)

    # "First boot": change leverage through the API.
    client1 = FakeClient()
    runtime1 = RuntimeConfig(settings, path=str(cfg), client=client1)
    tc1 = TestClient(create_app(settings=settings, client=client1, runtime=runtime1))
    login(tc1)
    assert tc1.post("/api/dashboard/config",
                    json={"leverage": 10, "margin_percent": 0.5}).status_code == 200

    # "Second boot": brand-new store + app reading the same persisted file.
    client2 = FakeClient()
    runtime2 = RuntimeConfig(settings, path=str(cfg), client=client2)
    tc2 = TestClient(create_app(settings=settings, client=client2, runtime=runtime2))
    login(tc2)
    state = tc2.get("/api/dashboard/state").json()
    assert state["leverage"] == 10
    assert state["margin_percent"] == pytest.approx(0.5)
