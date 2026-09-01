"""TKKBOT — TradingView alert webhook that trades USDT perps on Bybit."""
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import safety, signals
from .bybit_client import BybitClient, BybitError
from .config import Settings, get_settings
from .logger import setup_logging
from .safety import Cooldown, SafetyError

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, client: BybitClient | None = None) -> FastAPI:
    """Application factory. ``client`` is injectable so tests can mock Bybit."""
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    client = client or BybitClient(settings)
    cooldown = Cooldown(settings.cooldown_seconds)

    app = FastAPI(title="TKKBOT", version="1.0.0")

    @app.get("/")
    def root():
        return {"status": "ok", "service": "TKKBOT"}

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "trading_enabled": settings.trading_enabled,
            "testnet": settings.bybit_testnet,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/status")
    def status():
        return {
            "status": "ok",
            "testnet": settings.bybit_testnet,
            "trading_enabled": settings.trading_enabled,
            "allowed_symbols": sorted(settings.allowed_symbol_set),
            "max_qty_per_order": settings.max_qty_per_order,
            "max_notional_usd": settings.max_notional_usd,
            "max_leverage": settings.max_leverage,
            "default_leverage": settings.default_leverage,
            "cooldown_seconds": settings.cooldown_seconds,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.post("/webhook/tradingview")
    async def tradingview_webhook(request: Request):
        raw = (await request.body()).decode("utf-8", errors="replace")
        try:
            # Sync client code runs in a worker thread so the event loop stays free.
            return await run_in_threadpool(
                _handle_webhook, raw, settings, client, cooldown
            )
        except SafetyError as e:
            logger.warning("Rejected alert: %s", e.message)
            return JSONResponse({"error": e.message}, status_code=e.status_code)
        except BybitError as e:
            logger.error("Bybit error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=502)
        except signals.SignalError as e:
            logger.warning("Bad payload: %s", e.message)
            return JSONResponse({"error": e.message}, status_code=400)
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected error handling webhook")
            return JSONResponse({"error": "Internal error"}, status_code=500)

    return app


def _handle_webhook(raw: str, settings: Settings, client: BybitClient, cooldown: Cooldown):
    """Parse, validate, guardrail and execute one TradingView alert."""
    logger.info("Webhook received: %s", raw)

    data = signals.parse_payload(raw)
    signal = signals.build_signal(data, settings)

    safety.check_configuration(settings)
    safety.validate_secret(signal.secret, settings)
    safety.validate_trading_enabled(settings)
    safety.validate_symbol(signal.symbol, settings)
    safety.validate_leverage(signal.leverage or settings.default_leverage, settings)

    # Set the cooldown BEFORE any Bybit call: a TradingView retry (on 5xx) is
    # then swallowed here instead of double-trading.
    cooldown_key = signal.source_id or f"{signal.symbol}:{signal.side}"
    if not cooldown.check(cooldown_key):
        logger.info("Suppressed duplicate alert: %s", cooldown_key)
        return JSONResponse({"accepted": False, "reason": "cooldown"}, status_code=200)

    if signal.is_close:
        result = client.close_position(signal.symbol, qty=signal.qty)
        logger.info("Close executed: %s -> %s", signal.symbol, result)
        return JSONResponse(
            {"accepted": True, "action": "close", "symbol": signal.symbol, "result": result},
            status_code=200,
        )

    # Entry order: resolve qty (auto-size from balance if omitted), TP/SL, leverage.
    price = client.get_price(signal.symbol)
    if price is None:
        raise BybitError(f"Could not fetch current price for {signal.symbol}")

    qty = signal.qty
    if qty is None:
        balance = client.get_balance()
        margin = min(balance, settings.margin_usd_per_trade)
        leverage = signal.leverage or settings.default_leverage
        qty = (margin * leverage) / price
        logger.info(
            "Auto-sized qty %s for %s (margin=$%s x %sx, price=%s)",
            qty, signal.symbol, margin, leverage, price,
        )

    safety.validate_qty(qty, price, settings)

    leverage = signal.leverage or settings.default_leverage
    tp, sl = client.compute_tp_sl(signal, price)
    side = "Buy" if signal.side == "buy" else "Sell"
    result = client.place_market_order(
        signal.symbol, side, qty, leverage=leverage, tp=tp, sl=sl
    )

    logger.info("Entry executed: %s %s %s tp=%s sl=%s -> %s",
                signal.symbol, side, qty, tp, sl, result)
    return JSONResponse(
        {
            "accepted": True,
            "action": "entry",
            "symbol": signal.symbol,
            "side": side,
            "qty": qty,
            "leverage": leverage,
            "tp": tp,
            "sl": sl,
            "result": result,
        },
        status_code=200,
    )


settings = get_settings()
app = create_app(settings)
