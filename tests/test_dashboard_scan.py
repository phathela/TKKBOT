"""The dashboard's read-only window on the market scanner."""
import json

from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime_config import RuntimeConfig
from conftest import make_scan_settings, make_settings
from scanner.schemas import RankedItem, ScanResult
from scanner.storage import SqliteStore

PASSWORD = "test-secret"


class FakeClient:
    """Bybit stand-in: the dashboard reads balance and positions on every poll."""

    def get_balance(self, coin="USDT"):
        return 100.0

    def get_all_positions(self):
        return []


def build(tmp_path, **scan_overrides):
    settings = make_settings(webhook_secret=PASSWORD)
    client = FakeClient()
    runtime = RuntimeConfig(settings, path=str(tmp_path / "cfg.json"), client=client)
    scan_settings = make_scan_settings(
        scan_data_dir=str(tmp_path),
        scan_db_path=str(tmp_path / "scans.db"),
        **scan_overrides,
    )
    app = create_app(
        settings=settings, client=client, runtime=runtime,
        scan_settings=scan_settings,
    )
    tc = TestClient(app)
    assert tc.post("/dashboard/login", json={"password": PASSWORD}).status_code == 200
    return tc, scan_settings


def sample_result():
    return ScanResult(
        run_id="run-1",
        created_at=ScanResult.now_iso(),
        market_open=True,
        items=[
            RankedItem(
                rank=1, asset_type="stock", symbol="AMD", price=514.09,
                volatility_pct=9.76, direction_bias="bullish", confidence=62,
                thesis="Up 14% on the day.",
                key_risks=["No volume confirmation"],
                suggested_entry_zone="500-514", suggested_stop=480.0,
                suggested_target=560.0, time_horizon="1-3 days",
            )
        ],
        sources_used=["coingecko", "twelvedata"],
        new_symbols=["AMD"],
    )


def get(tc):
    response = tc.get("/api/dashboard/opportunities")
    assert response.status_code == 200, response.text
    return response


def test_opportunities_requires_a_session(tmp_path):
    tc, _ = build(tmp_path)
    tc.cookies.clear()
    assert tc.get("/api/dashboard/opportunities").status_code == 401


def test_no_scan_yet_is_a_state_not_an_error(tmp_path):
    tc, _ = build(tmp_path)
    body = get(tc).json()
    assert body["latest"] is None
    assert body["configured"] is True
    assert body["missing"] == []
    assert body["scan_times_utc"] == ["00:00", "06:00", "12:00", "18:00"]
    assert body["next_run"]
    assert body["scan_running"] is False


def test_the_stored_scan_is_served_in_full(tmp_path):
    tc, scan_settings = build(tmp_path)
    SqliteStore(scan_settings.db_path()).save(sample_result())

    body = get(tc).json()
    assert body["latest"]["run_id"] == "run-1"
    item = body["latest"]["items"][0]
    assert item["symbol"] == "AMD"
    assert item["suggested_stop"] == 480.0
    assert item["key_risks"] == ["No volume confirmation"]
    assert body["history"][0]["top"] == "AMD"
    assert body["history"][0]["pick_count"] == 1


def test_no_provider_key_reaches_the_browser(tmp_path):
    """The page is session-authenticated, so the scanner's keyed API stays keyed."""
    tc, scan_settings = build(tmp_path)
    SqliteStore(scan_settings.db_path()).save(sample_result())

    text = get(tc).text
    for secret in ("test-deepseek", "test-coingecko", "test-cmc",
                   "test-twelve", "test-api-key"):
        assert secret not in text


def test_unconfigured_scanner_names_what_is_missing(tmp_path):
    tc, _ = build(
        tmp_path,
        deepseek_api_key="", coingecko_api_key="", cmc_api_key="",
        twelvedata_api_key="", finnhub_api_key="",
    )
    body = get(tc).json()
    assert body["configured"] is False
    assert "DEEPSEEK_API_KEY" in body["missing"]
    assert "TWELVEDATA_API_KEY or FINNHUB_API_KEY" in body["missing"]


def test_a_bad_schedule_does_not_break_the_page(tmp_path):
    tc, _ = build(tmp_path, scan_times_utc="nonsense")
    body = get(tc).json()
    assert body["config_error"]
    assert body["scan_times_utc"] == []


def test_a_live_lock_shows_a_scan_running(tmp_path):
    tc, _ = build(tmp_path)
    (tmp_path / "scan.lock").write_text("1", encoding="utf-8")
    assert get(tc).json()["scan_running"] is True


def test_scheduler_state_is_reported(tmp_path):
    tc, _ = build(tmp_path, scan_scheduler_enabled=True)
    body = json.loads(get(tc).text)
    assert body["scheduler_enabled"] is True
