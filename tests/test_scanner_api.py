"""The /opportunities routes: keyed, fail-closed, and read-only."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from conftest import make_scan_settings

import scanner.api as scanner_api
from scanner.api import create_opportunities_router
from scanner.schemas import RankedItem, ScanResult
from scanner.storage import SqliteStore


def make_client(tmp_path, **settings_overrides):
    settings = make_scan_settings(
        scan_data_dir=str(tmp_path),
        scan_db_path=str(tmp_path / "scans.db"),
        **settings_overrides,
    )
    app = FastAPI()
    app.include_router(create_opportunities_router(settings))
    return TestClient(app), settings


def sample_result():
    return ScanResult(
        run_id="abc123",
        created_at=ScanResult.now_iso(),
        market_open=True,
        items=[
            RankedItem(
                rank=1, asset_type="crypto", symbol="BTC", price=60_000,
                volatility_pct=12.0, direction_bias="bullish", confidence=70,
                thesis="test",
            )
        ],
    )


def test_latest_requires_the_api_key(tmp_path):
    client, _ = make_client(tmp_path)
    assert client.get("/opportunities/latest").status_code == 401


def test_wrong_api_key_is_rejected(tmp_path):
    client, _ = make_client(tmp_path)
    response = client.get("/opportunities/latest", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


def test_unconfigured_key_fails_closed(tmp_path):
    client, _ = make_client(tmp_path, opportunities_api_key="")
    response = client.get(
        "/opportunities/latest", headers={"X-API-Key": "anything"}
    )
    assert response.status_code == 503


def test_latest_is_404_before_any_scan(tmp_path):
    client, settings = make_client(tmp_path)
    response = client.get(
        "/opportunities/latest", headers={"X-API-Key": settings.opportunities_api_key}
    )
    assert response.status_code == 404


def test_latest_returns_the_stored_scan(tmp_path):
    client, settings = make_client(tmp_path)
    SqliteStore(settings.db_path()).save(sample_result())
    response = client.get(
        "/opportunities/latest", headers={"X-API-Key": settings.opportunities_api_key}
    )
    assert response.status_code == 200
    assert response.json()["run_id"] == "abc123"


def test_history_returns_a_counted_list(tmp_path):
    client, settings = make_client(tmp_path)
    SqliteStore(settings.db_path()).save(sample_result())
    response = client.get(
        "/opportunities/history?days=7",
        headers={"X-API-Key": settings.opportunities_api_key},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["days"] == 7
    assert body["count"] == 1


def test_history_days_is_bounded(tmp_path):
    client, settings = make_client(tmp_path)
    response = client.get(
        "/opportunities/history?days=900",
        headers={"X-API-Key": settings.opportunities_api_key},
    )
    assert response.status_code == 422


def test_scan_returns_202_and_schedules_the_scan(tmp_path, monkeypatch):
    calls = []

    async def fake_task(settings, store):
        calls.append(True)

    monkeypatch.setattr(scanner_api, "_scan_task", fake_task)
    client, settings = make_client(tmp_path)
    response = client.post(
        "/opportunities/scan", headers={"X-API-Key": settings.opportunities_api_key}
    )
    assert response.status_code == 202
    assert response.json()["started"] is True
    # TestClient runs added background tasks after sending the response.
    assert calls == [True]


def test_scan_refuses_to_run_without_configuration(tmp_path):
    client, _ = make_client(
        tmp_path,
        deepseek_api_key="",
        coingecko_api_key="",
        cmc_api_key="",
        twelvedata_api_key="",
        finnhub_api_key="",
    )
    response = client.post(
        "/opportunities/scan", headers={"X-API-Key": "test-api-key"}
    )
    assert response.status_code == 503
    assert "DEEPSEEK_API_KEY" in response.json()["detail"]
