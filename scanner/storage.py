"""Scan persistence: SQLite when a path is usable, JSON files otherwise.

Every scan is stored whole (metrics + LLM output + sources used), so a result
can be explained later without re-running anything. A failed write must never
abort a scan that already produced its ranking — callers get a logged error,
not an exception.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .schemas import ScanResult

logger = logging.getLogger(__name__)


class ScanStore:
    """Interface implemented by both back ends."""

    def save(self, result: ScanResult) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def latest(self) -> dict | None:  # pragma: no cover - interface
        raise NotImplementedError

    def history(self, *, days: int = 7) -> list[dict]:  # pragma: no cover
        raise NotImplementedError

    def recent_symbols(self, *, scans: int = 1) -> set[str]:
        records = self.history(days=30)[:scans]
        symbols: set[str] = set()
        for record in records:
            for item in record.get("items", []):
                symbol = item.get("symbol")
                if symbol:
                    symbols.add(str(symbol).upper())
        return symbols


class SqliteStore(ScanStore):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scans (
                    run_id     TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    payload    TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS scans_created_at ON scans(created_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def save(self, result: ScanResult) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO scans (run_id, created_at, payload) VALUES (?,?,?)",
                (result.run_id, result.created_at, result.model_dump_json()),
            )

    def latest(self) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM scans ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def history(self, *, days: int = 7) -> list[dict]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, days))
        ).replace(microsecond=0).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM scans WHERE created_at >= ? "
                "ORDER BY created_at DESC",
                (cutoff,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]


class JsonFileStore(ScanStore):
    """Fallback when SQLite is unavailable (read-only mount, missing driver)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.latest_path = directory / "opportunities_latest.json"
        self.history_path = directory / "opportunities_history.jsonl"

    def save(self, result: ScanResult) -> None:
        payload = result.model_dump_json()
        self.latest_path.write_text(payload, encoding="utf-8")
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")

    def latest(self) -> dict | None:
        try:
            return json.loads(self.latest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Latest scan file unreadable: %s", exc)
            return None

    def history(self, *, days: int = 7) -> list[dict]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, days))
        ).replace(microsecond=0).isoformat()
        records: list[dict] = []
        try:
            lines = self.history_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("History file unreadable: %s", exc)
            return []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(record.get("created_at", "")) >= cutoff:
                records.append(record)
        return records


def open_store(db_path: Path | None, data_dir: Path) -> ScanStore:
    """Prefer SQLite; fall back to JSON files with a clear log line."""
    if db_path is not None:
        try:
            store = SqliteStore(db_path)
            logger.info("Scan storage: sqlite at %s", db_path)
            return store
        except (sqlite3.Error, OSError) as exc:
            logger.warning("SQLite unavailable at %s (%s); using JSON files", db_path, exc)
    store = JsonFileStore(data_dir)
    logger.info("Scan storage: JSON files in %s", data_dir)
    return store
