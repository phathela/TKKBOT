"""Read/trigger endpoints for scanned opportunities.

Mounted on the existing app by :func:`app.main.create_app`. Every route needs
``X-API-Key: $OPPORTUNITIES_API_KEY``; when that variable is unset the routes
fail closed with 503 rather than being open. Nothing here can place an order.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from .config import ConfigError, ScanSettings, get_scan_settings
from .lock import LockBusy
from .run import Scanner
from .storage import ScanStore, open_store

logger = logging.getLogger(__name__)


def create_opportunities_router(settings: ScanSettings | None = None) -> APIRouter:
    router = APIRouter(prefix="/opportunities", tags=["opportunities"])
    store_holder: dict[str, ScanStore] = {}

    def current_settings() -> ScanSettings:
        return settings or get_scan_settings()

    def get_store() -> ScanStore:
        # Built lazily: importing the app must not touch the filesystem.
        if "store" not in store_holder:
            scan_settings = current_settings()
            store_holder["store"] = open_store(
                scan_settings.db_path(), scan_settings.data_dir()
            )
        return store_holder["store"]

    def require_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
        expected = current_settings().opportunities_api_key
        if not expected:
            raise HTTPException(
                status_code=503,
                detail="OPPORTUNITIES_API_KEY is not configured",
            )
        if not x_api_key or not hmac.compare_digest(x_api_key, expected):
            raise HTTPException(status_code=401, detail="Invalid API key")

    @router.get("/latest", dependencies=[Depends(require_key)])
    def latest() -> dict:
        record = get_store().latest()
        if record is None:
            raise HTTPException(status_code=404, detail="No scan has run yet")
        return record

    @router.get("/history", dependencies=[Depends(require_key)])
    def history(days: int = Query(default=7, ge=1, le=90)) -> dict:
        scans = get_store().history(days=days)
        return {"days": days, "count": len(scans), "scans": scans}

    @router.post("/scan", status_code=202, dependencies=[Depends(require_key)])
    async def scan_now(background: BackgroundTasks) -> dict:
        """Start a scan and return at once; the run takes several minutes.

        It cannot be synchronous: a scan outlives Railway's ~60 s HTTP timeout
        (Twelve Data's free tier is paced at one request a minute), so the
        caller would always see a dropped connection. Poll
        ``/opportunities/latest`` for the result instead.
        """
        scan_settings = current_settings()
        try:
            scan_settings.ensure_ready()
        except ConfigError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        background.add_task(_scan_task, scan_settings, get_store())
        return {
            "started": True,
            "detail": "Scan started; poll /opportunities/latest in a few minutes.",
        }

    return router


async def _scan_task(settings: ScanSettings, store: ScanStore) -> None:
    """Run the scan off the event loop, after the 202 has been sent."""
    try:
        await run_in_threadpool(_run_scan, settings, store)
    except LockBusy:
        logger.info("Scan trigger ignored: another scan is already running")
    except ConfigError as exc:
        logger.error("Triggered scan not configured: %s", exc)
    except Exception:  # noqa: BLE001 — a background scan must never crash the app
        logger.exception("Triggered scan failed")


def _run_scan(settings: ScanSettings, store: ScanStore):
    return Scanner(settings, store=store).run()
