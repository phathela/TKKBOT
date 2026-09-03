"""Dashboard: web UI + API for adjusting TKKBOT's live trading settings.

Serves the single-page front-end at ``/dashboard`` and JSON endpoints under
``/api/dashboard``. Every API route except login requires a session cookie
signed with the dashboard secret (``DASHBOARD_PASSWORD``, else
``WEBHOOK_SECRET``). Password/credentials are never returned by any route.
"""
import hashlib
import hmac
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from .bybit_client import BybitClient, BybitError
from .config import Settings
from .runtime_config import ConfigError, RuntimeConfig

logger = logging.getLogger(__name__)

_COOKIE = "tkkbot_session"
_COOKIE_TTL = 7 * 24 * 3600  # 7 days
_HTML = Path(__file__).resolve().parent / "static" / "dashboard.html"


def _sign(secret: str, expiry: str) -> str:
    return hmac.new(secret.encode("utf-8"), expiry.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _make_session(secret: str) -> str:
    expiry = str(int(time.time()) + _COOKIE_TTL)
    return f"{expiry}.{_sign(secret, expiry)}"


def _valid_session(secret: str, value: str) -> bool:
    try:
        expiry, sig = value.rsplit(".", 1)
        int(expiry)  # must be numeric
    except (ValueError, AttributeError):
        return False
    expected = _sign(secret, expiry)
    if not hmac.compare_digest(sig, expected):
        return False
    return int(expiry) > time.time()


def create_dashboard_router(
    settings: Settings, client: BybitClient, runtime: RuntimeConfig
) -> APIRouter:
    secret = settings.dashboard_password or settings.webhook_secret
    router = APIRouter()

    def require_session(request: Request) -> None:
        value = request.cookies.get(_COOKIE)
        if not value or not _valid_session(secret, value):
            raise HTTPException(status_code=401, detail="Not signed in")

    @router.get("/dashboard")
    def page():
        if not _HTML.exists():
            raise HTTPException(status_code=404, detail="dashboard page missing")
        return FileResponse(str(_HTML), media_type="text/html")

    @router.post("/dashboard/login")
    def login(request: Request, payload: dict = Body(...)):
        if not secret:
            raise HTTPException(
                status_code=503,
                detail="Dashboard not configured: no WEBHOOK_SECRET set",
            )
        password = str(payload.get("password") or "")
        if not hmac.compare_digest(password, secret):
            raise HTTPException(status_code=401, detail="Wrong password")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            _COOKIE,
            _make_session(secret),
            max_age=_COOKIE_TTL,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    @router.post("/dashboard/logout")
    def logout(response: Response):
        response.delete_cookie(_COOKIE, path="/")
        return {"ok": True}

    @router.get("/api/dashboard/state", dependencies=[Depends(require_session)])
    def state():
        payload = runtime.to_dict()
        payload["testnet"] = settings.bybit_testnet
        payload["coin"] = "USDT"

        # Live account data fails soft: the tunables above still render even if
        # Bybit is unreachable, and the UI shows a clear "unavailable" banner.
        payload["live_status"] = "error"
        payload["live_error"] = None
        payload["wallet_balance"] = None
        payload["positions"] = []
        try:
            allowed = set(runtime.allowed)
            payload["wallet_balance"] = client.get_balance("USDT")
            payload["positions"] = [
                p for p in client.get_all_positions() if p["symbol"] in allowed
            ]
            payload["live_status"] = "ok"
        except BybitError as e:
            payload["live_error"] = str(e)
        except Exception as e:  # noqa: BLE001
            logger.exception("Dashboard state: live reads failed")
            payload["live_error"] = f"{type(e).__name__}: {e}"
        return payload

    @router.post("/api/dashboard/config", dependencies=[Depends(require_session)])
    def update_config(payload: dict = Body(...)):
        try:
            return runtime.update(payload)
        except ConfigError as e:
            raise HTTPException(status_code=400, detail=e.message)

    @router.get("/api/dashboard/pairs", dependencies=[Depends(require_session)])
    def pairs(q: str = "", limit: int = 50):
        try:
            names = client.list_pairs(query=q, limit=limit)
        except BybitError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"pairs": names}

    @router.post("/api/dashboard/close", dependencies=[Depends(require_session)])
    def close_position(payload: dict = Body(...)):
        symbol = str(payload.get("symbol") or "").upper()
        if symbol not in runtime.allowed_symbol_set:
            raise HTTPException(status_code=400, detail=f"{symbol} is not an allowed pair")
        try:
            return client.close_position(symbol)
        except BybitError as e:
            logger.warning("Dashboard close failed for %s: %s", symbol, e)
            raise HTTPException(status_code=502, detail=str(e))

    return router
