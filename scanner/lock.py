"""Single-flight guard so two scans never overlap.

A Railway cron and a manual ``POST /opportunities/scan`` can fire at the same
moment; the lock makes the second caller back off instead of spending API
credits twice. The lock lives on the volume, so it also works across restarts —
a stale lock (crashed run) expires after ``stale_after_seconds``.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class ScanLock:
    def __init__(self, path: Path, *, stale_after_seconds: int = 30 * 60) -> None:
        self.path = path
        self.stale_after_seconds = stale_after_seconds

    def acquire(self) -> bool:
        """True when this caller now owns the lock."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._is_stale():
                logger.warning("Removing stale scan lock at %s", self.path)
                self.release()
                return self.acquire()
            return False
        except OSError as exc:
            # A broken volume must not block scanning entirely.
            logger.warning("Could not create scan lock (%s); continuing", exc)
            return True
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(int(time.time())))
        return True

    def release(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not remove scan lock: %s", exc)

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return True
        return age > self.stale_after_seconds

    def __enter__(self) -> "ScanLock":
        if not self.acquire():
            raise LockBusy(f"a scan is already running ({self.path})")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


class LockBusy(RuntimeError):
    """Another scan owns the lock."""
