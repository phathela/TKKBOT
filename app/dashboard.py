"""Dashboard: web UI + API for adjusting TKKBOT's live trading settings.

Serves the single-page front-end at ``/dashboard`` and JSON endpoints under
``/api/dashboard``. Every API route except login requires a session cookie
signed with the dashboard secret (``DASHBOARD_PASSWORD``, else
``WEBHOOK_SECRET``). Password/credentials are never returned by any route.
"""
import hashlib
import hmac
import json
import logging
import re
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

    @router.get("/api/dashboard/alert-template", dependencies=[Depends(require_session)])
    def alert_template(request: Request, symbol: str = ""):
        """A ready-to-paste webhook URL and alert body for one pair.

        Deliberately *not* part of ``/api/dashboard/state``: that route is polled
        every 10s, and this response carries the webhook secret, so it is fetched
        only when the user asks for a specific pair.
        """
        symbol = symbol.strip().upper()
        if symbol not in runtime.allowed_symbol_set:
            raise HTTPException(status_code=400, detail=f"{symbol} is not an allowed pair")
        if not settings.webhook_secret:
            raise HTTPException(
                status_code=503,
                detail="No WEBHOOK_SECRET configured, so there is no alert to copy",
            )
        label = runtime.pair_label(symbol)
        qty = runtime.pair_qty(symbol)
        # Omit "qty" when the pair has none saved: the alert would fail rather
        # than fall back, and seeing that in the template is the point.
        body = {"secret": settings.webhook_secret, "symbol": symbol, "side": "buy"}
        if qty is not None:
            body["qty"] = qty
        return JSONResponse(
            {
                "symbol": symbol,
                "label": label,
                "qty": qty,
                "webhook_url": _webhook_url(request, settings),
                "payload_text": json.dumps(body, indent=2),
            },
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/dashboard/close", dependencies=[Depends(require_session)])
    def close_position(payload: dict = Body(...)):
        symbol = str(payload.get("symbol") or "").upper()
        if symbol not in runtime.allowed_symbol_set:
            raise HTTPException(status_code=400, detail=f"{symbol} is not an allowed pair")
        try:
            return client.close_position(symbol, label=runtime.pair_label(symbol))
        except BybitError as e:
            logger.warning("Dashboard close failed for %s: %s", symbol, e)
            raise HTTPException(status_code=502, detail=str(e))

    return router


# Hosts that are never TLS-terminated in front of us: a local or LAN run keeps
# whatever scheme it was reached on.
_PRIVATE_HOST_RE = re.compile(
    r"^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)"
)


def _webhook_url(request: Request, settings: Settings) -> str:
    """The public webhook URL to put in a TradingView alert.

    ``PUBLIC_BASE_URL`` wins, because the request-derived fallback is not
    dependable behind a proxy: uvicorn trusts ``X-Forwarded-*`` only from
    ``forwarded_allow_ips``, and outside that it resolves ``base_url`` to the
    container's own bind address (``http://0.0.0.0:8000/``). The Procfile passes
    ``--forwarded-allow-ips=*`` so the fallback works too; the setting remains
    the belt-and-braces for any other host.
    """
    base = (settings.public_base_url or "").strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
        # A public host is TLS-terminated upstream, so an http:// base means the
        # proxy headers were dropped. Left as-is, that URL gets pasted into an
        # alert where the redirect to https can turn the POST into a GET.
        host = base.split("://", 1)[-1].split("/", 1)[0]
        if base.startswith("http://") and not _PRIVATE_HOST_RE.match(host):
            base = "https://" + base[len("http://"):]
    return f"{base}/webhook/tradingview"
