"""Read/trigger endpoints for scanned opportunities.

Mounted on the existing app by :func:`app.main.create_app`. Every route needs
``X-API-Key: $OPPORTUNITIES_API_KEY``; when that variable is unset the routes
fail closed with 503 rather than being open. Nothing here can place an order.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query
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

    @router.post("/scan", dependencies=[Depends(require_key)])
    async def scan_now() -> dict:
        scan_settings = current_settings()
        try:
            scan_settings.ensure_ready()
        except ConfigError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            result = await run_in_threadpool(
                _run_scan, scan_settings, get_store()
            )
        except LockBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ConfigError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return result.model_dump()

    return router


def _run_scan(settings: ScanSettings, store: ScanStore):
    return Scanner(settings, store=store).run()
