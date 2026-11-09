"""Optional in-process scheduler for the four daily scans.

A dedicated Railway cron service running ``python -m scanner.run`` is the
preferred setup (one scanner process, no duplicated work across web replicas).
Set ``SCAN_SCHEDULER_ENABLED=true`` to run the loop inside the web app instead;
the file lock in :mod:`scanner.lock` makes a double schedule harmless.
"""
from __future__ import annotations

import logging
import threading
import time as time_module
from datetime import datetime, time, timedelta, timezone

from .config import ScanSettings, get_scan_settings, log_settings_summary

logger = logging.getLogger(__name__)

_started = False
_lock = threading.Lock()


def next_run_after(moment: datetime, times: list[time]) -> datetime:
    """The first scheduled UTC time strictly after ``moment``."""
    moment = moment.astimezone(timezone.utc)
    for scheduled in sorted(times):
        candidate = datetime.combine(
            moment.date(), scheduled, tzinfo=timezone.utc
        )
        if candidate > moment:
            return candidate
    first = datetime.combine(
        moment.date() + timedelta(days=1), sorted(times)[0], tzinfo=timezone.utc
    )
    return first


class ScanLoop(threading.Thread):
    def __init__(self, settings: ScanSettings, *, stop_event: threading.Event) -> None:
        super().__init__(name="scanner-scheduler", daemon=True)
        self._settings = settings
        self._stop = stop_event

    def run(self) -> None:  # pragma: no cover - timing loop
        from .lock import LockBusy
        from .run import Scanner

        while not self._stop.is_set():
            target = next_run_after(datetime.now(timezone.utc), self._settings.scan_times())
            wait_seconds = max(1.0, (target - datetime.now(timezone.utc)).total_seconds())
            logger.info("Next scan scheduled at %s (in %.0f min)",
                        target.isoformat(), wait_seconds / 60)
            if self._stop.wait(wait_seconds):
                return
            try:
                Scanner(self._settings).run()
            except LockBusy:
                logger.info("Skipping scheduled scan: another run holds the lock")
            except Exception:  # noqa: BLE001 — the loop must survive anything
                logger.exception("Scheduled scan failed")


def start_if_enabled(settings: ScanSettings | None = None) -> bool:
    """Start the loop when enabled. Never raises — the web app must boot."""
    global _started
    try:
        settings = settings or get_scan_settings()
        if not settings.scan_scheduler_enabled:
            return False
        with _lock:
            if _started:
                return True
            log_settings_summary(settings)
            ScanLoop(settings, stop_event=threading.Event()).start()
            _started = True
            logger.info("In-app scan scheduler started")
            return True
    except Exception:  # noqa: BLE001
        logger.exception("Could not start the scan scheduler; use a cron service")
        return False


def main() -> int:  # pragma: no cover - long-running entry point
    from .run import _setup_logging

    settings = get_scan_settings()
    _setup_logging(settings.log_level)
    stop = threading.Event()
    logger.info("Scanner scheduler running in the foreground (Ctrl-C to stop)")
    ScanLoop(settings, stop_event=stop).start()
    try:
        while True:
            time_module.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
        logger.info("Scheduler stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
