"""Pipeline glue: the lock, market hours, settings validation, the dry run."""
import os
import time
from datetime import datetime, timezone

import pytest
from conftest import make_scan_settings

from scanner.config import ConfigError
from scanner.lock import LockBusy, ScanLock
from scanner.run import Scanner, is_us_market_open
from scanner.scheduler import next_run_after


# --- lock ------------------------------------------------------------------


def test_lock_is_single_flight(tmp_path):
    first = ScanLock(tmp_path / "scan.lock")
    second = ScanLock(tmp_path / "scan.lock")
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_lock_context_manager_releases(tmp_path):
    path = tmp_path / "scan.lock"
    with ScanLock(path):
        assert path.exists()
        with pytest.raises(LockBusy):
            with ScanLock(path):
                pass
    assert not path.exists()


def test_stale_lock_is_reclaimed(tmp_path):
    path = tmp_path / "scan.lock"
    path.write_text("0")
    old = time.time() - 60 * 60
    os.utime(path, (old, old))
    assert ScanLock(path, stale_after_seconds=60).acquire() is True


# --- market hours ----------------------------------------------------------


@pytest.mark.parametrize(
    "moment,expected",
    [
        (datetime(2025, 6, 11, 13, 0, tzinfo=timezone.utc), False),  # 09:00 ET
        (datetime(2025, 6, 11, 13, 30, tzinfo=timezone.utc), True),  # 09:30 ET
        (datetime(2025, 6, 11, 18, 0, tzinfo=timezone.utc), True),   # 14:00 ET
        (datetime(2025, 6, 11, 21, 0, tzinfo=timezone.utc), False),  # 17:00 ET
        (datetime(2025, 6, 14, 16, 0, tzinfo=timezone.utc), False),  # Saturday
    ],
)
def test_us_market_hours(moment, expected):
    assert is_us_market_open(moment) is expected


# --- scheduler -------------------------------------------------------------


def test_next_run_is_later_the_same_day():
    from datetime import time

    moment = datetime(2025, 6, 11, 5, 0, tzinfo=timezone.utc)
    times = [time(0), time(6), time(12), time(18)]
    assert next_run_after(moment, times) == datetime(
        2025, 6, 11, 6, 0, tzinfo=timezone.utc
    )


def test_next_run_rolls_over_to_tomorrow():
    from datetime import time

    moment = datetime(2025, 6, 11, 23, 0, tzinfo=timezone.utc)
    times = [time(0), time(6), time(12), time(18)]
    assert next_run_after(moment, times) == datetime(
        2025, 6, 12, 0, 0, tzinfo=timezone.utc
    )


def test_next_run_skips_the_exact_moment():
    from datetime import time

    moment = datetime(2025, 6, 11, 6, 0, tzinfo=timezone.utc)
    assert next_run_after(moment, [time(6), time(12)]) == datetime(
        2025, 6, 11, 12, 0, tzinfo=timezone.utc
    )


# --- settings --------------------------------------------------------------


def test_missing_required_keys_are_named_not_valued():
    settings = make_scan_settings(
        deepseek_api_key="", coingecko_api_key="", cmc_api_key="",
        twelvedata_api_key="", finnhub_api_key="",
    )
    missing = settings.missing_required()
    assert "DEEPSEEK_API_KEY" in missing
    assert "TWELVEDATA_API_KEY or FINNHUB_API_KEY" in missing
    with pytest.raises(ConfigError, match="missing"):
        settings.ensure_ready()


def test_either_stock_provider_satisfies_the_requirement():
    settings = make_scan_settings(twelvedata_api_key="", finnhub_api_key="fh")
    assert settings.missing_required() == []


def test_dry_run_needs_no_keys():
    settings = make_scan_settings(
        deepseek_api_key="", coingecko_api_key="", cmc_api_key="",
        twelvedata_api_key="", finnhub_api_key="",
    )
    assert settings.missing_required(dry_run=True) == []


def test_scan_times_are_parsed_and_sorted():
    times = make_scan_settings(scan_times_utc="18:00, 00:00,06:30").scan_times()
    assert [(t.hour, t.minute) for t in times] == [(0, 0), (6, 30), (18, 0)]


def test_invalid_scan_time_is_a_config_error():
    with pytest.raises(ConfigError, match="invalid time"):
        make_scan_settings(scan_times_utc="25:99").scan_times()


def test_supported_database_url_becomes_a_sqlite_path():
    settings = make_scan_settings(database_url="sqlite:////data/scans.db")
    assert settings.db_path().as_posix() == "/data/scans.db"


def test_unsupported_database_url_falls_back_to_sqlite():
    settings = make_scan_settings(database_url="postgres://nope", scan_data_dir="x")
    assert settings.db_path().name == "opportunities.db"


# --- dry run ---------------------------------------------------------------


def test_dry_run_needs_no_network_and_returns_a_full_ranking(tmp_path):
    settings = make_scan_settings(
        scan_data_dir=str(tmp_path),
        deepseek_api_key="", coingecko_api_key="", cmc_api_key="",
    )
    result = Scanner(settings).run(dry_run=True, notify=False)

    assert result.dry_run is True
    assert result.llm_fallback is True
    assert len(result.items) == 10
    assert sum(1 for i in result.items if i.asset_type == "crypto") == 5
    assert sum(1 for i in result.items if i.asset_type == "stock") == 5
    assert [i.rank for i in result.items] == list(range(1, 11))
    assert all(i.symbol not in {"USDT", "WBTC"} for i in result.items)
    assert not (tmp_path / "opportunities_latest.json").exists()
    # BTC is analysed even when it does not rank, so the dashboard can pin it.
    assert result.anchor is not None
    assert result.anchor.symbol == "BTC"
    assert result.anchor.price == 67_120.0


def test_dry_run_marks_new_symbols_against_the_store(tmp_path):
    settings = make_scan_settings(scan_data_dir=str(tmp_path),
                                  deepseek_api_key="")
    Scanner(settings).run(dry_run=True, notify=False)
    second = Scanner(settings).run(dry_run=True, notify=False)
    # Nothing is stored by a dry run, so everything is new every time.
    assert len(second.new_symbols) == 10
