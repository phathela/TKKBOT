"""A read-only scanner snapshot for the trading dashboard.

The dashboard is session-authenticated; the scanner's own routes are keyed with
``X-API-Key``. This lets the one page show the scan without shipping
``OPPORTUNITIES_API_KEY`` to a browser. Read-only by construction: nothing here
can start a scan or place an order, and no provider key is ever included — the
payload carries rankings, source names and times only.
"""
from __future__ import annotations

import logging
import time as time_module
from datetime import datetime, timezone

from .config import ConfigError, ScanSettings, get_scan_settings
from .lock import ScanLock
from .scheduler import next_run_after
from .storage import ScanStore, open_store

logger = logging.getLogger(__name__)

# How many recent runs the "recent scans" strip shows. Anything older than a
# day is not interesting on a dashboard that already shows the latest in full.
HISTORY_PREVIEW = 5


class ScanDashboard:
    """Lazily opens the scan store, so importing the app never touches disk."""

    def __init__(self, settings: ScanSettings | None = None) -> None:
        self._settings = settings
        self._store: ScanStore | None = None

    def _scan_settings(self) -> ScanSettings:
        return self._settings or get_scan_settings()

    def _open_store(self, settings: ScanSettings) -> ScanStore:
        if self._store is None:
            self._store = open_store(settings.db_path(), settings.data_dir())
        return self._store

    def snapshot(self) -> dict:
        settings = self._scan_settings()
        snapshot: dict = {
            "configured": False,
            "missing": [],
            "config_error": None,
            "scheduler_enabled": settings.scan_scheduler_enabled,
            "scan_times_utc": [],
            "next_run": None,
            "scan_running": False,
            "storage_error": None,
            "latest": None,
            "history": [],
        }

        # Names only, never values.
        snapshot["missing"] = settings.missing_required()
        snapshot["configured"] = not snapshot["missing"]

        try:
            times = settings.scan_times()
        except ConfigError as exc:
            # A bad SCAN_TIMES_UTC costs the schedule, not the page.
            snapshot["config_error"] = str(exc)
        else:
            snapshot["scan_times_utc"] = [moment.strftime("%H:%M") for moment in times]
            snapshot["next_run"] = next_run_after(
                datetime.now(timezone.utc), times
            ).isoformat()

        try:
            store = self._open_store(settings)
            latest = store.latest()
            history = store.history(days=1)
        except Exception as exc:  # noqa: BLE001 — a read failure must not 500 the page
            logger.warning("Dashboard: scan store unreadable: %s", exc)
            snapshot["storage_error"] = f"{type(exc).__name__}: {exc}"
        else:
            snapshot["latest"] = latest
            snapshot["history"] = _preview(history)

        snapshot["scan_running"] = _scan_running(settings)
        return snapshot


def _scan_running(settings: ScanSettings) -> bool:
    """True while a scan holds its lock file (and the lock is not stale)."""
    lock = ScanLock(settings.data_dir() / "scan.lock")
    try:
        age = time_module.time() - lock.path.stat().st_mtime
    except OSError:
        return False
    return age <= lock.stale_after_seconds


def _preview(history: list[dict]) -> list[dict]:
    """Just enough of each run for the strip; the full payloads stay server-side."""
    runs: list[dict] = []
    for record in history[:HISTORY_PREVIEW]:
        items = record.get("items") or []
        runs.append(
            {
                "run_id": record.get("run_id"),
                "created_at": record.get("created_at"),
                "llm_fallback": bool(record.get("llm_fallback")),
                "dry_run": bool(record.get("dry_run")),
                "pick_count": len(items),
                "top": items[0].get("symbol") if items else None,
                "new_symbols": record.get("new_symbols") or [],
            }
        )
    return runs
