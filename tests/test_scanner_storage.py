"""Scan persistence: SQLite primary, JSON fallback, identical behaviour."""
from datetime import datetime, timedelta, timezone

import pytest

from scanner.schemas import RankedItem, ScanResult
from scanner.storage import JsonFileStore, SqliteStore, open_store


def result(run_id="run-1", *, days_ago=0, symbols=("BTC", "NVDA")):
    created = (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).replace(microsecond=0).isoformat()
    return ScanResult(
        run_id=run_id,
        created_at=created,
        market_open=True,
        items=[
            RankedItem(
                rank=index,
                asset_type="crypto" if index == 1 else "stock",
                symbol=symbol,
                price=100.0,
                volatility_pct=10.0,
                direction_bias="bullish",
                confidence=70,
                thesis="test",
            )
            for index, symbol in enumerate(symbols, start=1)
        ],
    )


@pytest.fixture(params=["sqlite", "json"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return SqliteStore(tmp_path / "scans.db")
    return JsonFileStore(tmp_path / "json")


def test_latest_is_none_before_any_scan(store):
    assert store.latest() is None


def test_save_then_latest_round_trips(store):
    store.save(result())
    latest = store.latest()
    assert latest["run_id"] == "run-1"
    assert [item["symbol"] for item in latest["items"]] == ["BTC", "NVDA"]


def test_latest_is_the_newest_scan(store):
    store.save(result("old", days_ago=2))
    store.save(result("new"))
    assert store.latest()["run_id"] == "new"


def test_history_is_newest_first(store):
    store.save(result("old", days_ago=2))
    store.save(result("new"))
    assert [scan["run_id"] for scan in store.history(days=7)] == ["new", "old"]


def test_history_respects_the_window(store):
    store.save(result("old", days_ago=10))
    store.save(result("new"))
    assert [scan["run_id"] for scan in store.history(days=7)] == ["new"]


def test_recent_symbols_reads_the_last_scan(store):
    store.save(result("old", days_ago=1, symbols=("ETH",)))
    store.save(result("new", symbols=("BTC", "NVDA")))
    assert store.recent_symbols(scans=1) == {"BTC", "NVDA"}


def test_recent_symbols_can_look_further_back(store):
    store.save(result("old", days_ago=1, symbols=("ETH",)))
    store.save(result("new", symbols=("BTC",)))
    assert store.recent_symbols(scans=2) == {"BTC", "ETH"}


def test_saving_the_same_run_twice_does_not_duplicate_in_sqlite(tmp_path):
    sqlite = SqliteStore(tmp_path / "scans.db")
    sqlite.save(result())
    sqlite.save(result())
    assert len(sqlite.history(days=7)) == 1


def test_open_store_prefers_sqlite(tmp_path):
    assert isinstance(open_store(tmp_path / "x.db", tmp_path), SqliteStore)


def test_open_store_falls_back_to_json_without_a_path(tmp_path):
    assert isinstance(open_store(None, tmp_path / "data"), JsonFileStore)


def test_json_store_tolerates_a_corrupt_latest_file(tmp_path):
    json_store = JsonFileStore(tmp_path)
    json_store.latest_path.write_text("{broken", encoding="utf-8")
    assert json_store.latest() is None


def test_json_store_skips_corrupt_history_lines(tmp_path):
    json_store = JsonFileStore(tmp_path)
    json_store.save(result("good"))
    with json_store.history_path.open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    assert [scan["run_id"] for scan in json_store.history(days=7)] == ["good"]
