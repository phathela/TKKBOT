"""TKKBOT — TradingView alert webhook that trades USDT perps on Bybit."""
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import safety, signals
from .bybit_client import BybitClient, BybitError
from .config import Settings, get_settings
from .dashboard import create_dashboard_router
from .logger import setup_logging
from .runtime_config import RuntimeConfig
from .safety import Cooldown, SafetyError

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    client: BybitClient | None = None,
    runtime: RuntimeConfig | None = None,
) -> FastAPI:
    """Application factory. ``client``/``runtime`` are injectable so tests can mock them."""
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    client = client or BybitClient(settings)
    cooldown = Cooldown(settings.cooldown_seconds)
    runtime = runtime or RuntimeConfig(settings, client=client)

    app = FastAPI(title="TKKBOT", version="1.0.0")

    @app.get("/")
    def root():
        return {"status": "ok", "service": "TKKBOT", "dashboard": "/dashboard"}

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "trading_enabled": runtime.trading_enabled,
            "testnet": settings.bybit_testnet,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/status")
    def status():
        return {
            "status": "ok",
            "testnet": settings.bybit_testnet,
            "trading_enabled": runtime.trading_enabled,
            "allowed_symbols": sorted(runtime.allowed),
            "max_qty_per_order": settings.max_qty_per_order,
            "max_notional_usd": settings.max_notional_usd,
            "max_leverage": runtime.max_leverage,
            "leverage": runtime.leverage,
            "sl_percent": runtime.sl_percent,
            "tp_percent": runtime.tp_percent,
            "margin_percent": runtime.margin_percent,
            "cooldown_seconds": settings.cooldown_seconds,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    app.include_router(create_dashboard_router(settings, client, runtime))

    @app.post("/webhook/tradingview")
    async def tradingview_webhook(request: Request):
        raw = (await request.body()).decode("utf-8", errors="replace")
        try:
            # Sync client code runs in a worker thread so the event loop stays free.
            return await run_in_threadpool(
                _handle_webhook, raw, settings, client, runtime, cooldown
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


def _handle_webhook(
    raw: str,
    settings: Settings,
    client: BybitClient,
    runtime: RuntimeConfig,
    cooldown: Cooldown,
):
    """Parse, validate, guardrail and execute one TradingView alert."""
    logger.info("Webhook received: %s", raw)

    data = signals.parse_payload(raw)
    signal = signals.build_signal(data, settings)

    safety.check_configuration(settings)
    safety.validate_secret(signal.secret, settings)
    safety.validate_trading_enabled(runtime)
    safety.validate_symbol(signal.symbol, runtime)

    # The dashboard is authoritative for leverage; ignore whatever the alert says
    # (their alerts carry "leverage": 5, which would otherwise defeat the dashboard).
    if signal.leverage is not None and signal.leverage != runtime.leverage:
        logger.info(
            "Ignoring alert leverage %sx — dashboard controls leverage (%sx)",
            signal.leverage, runtime.leverage,
        )
    leverage = runtime.leverage

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

    # Directional alert: force the position to MATCH the signal side, so the bot
    # is always in the market — long on "buy", short on "sell". If an opposite
    # position is open we close it (reduce-only) and open the target side; a
    # same-direction alert while already positioned is a hold (no double).
    position = client.get_position(signal.symbol)
    target_side = "Buy" if signal.side == "buy" else "Sell"
    if position is not None:
        if position["side"] == target_side:
            logger.info(
                "Already %s %s (qty=%s, avg=%s) — holding on %s signal",
                position["side"], signal.symbol, position["size"],
                position["avg_price"], signal.side,
            )
            return JSONResponse(
                {
                    "accepted": True,
                    "action": "hold",
                    "symbol": signal.symbol,
                    "side": position["side"],
                    "reason": "already in this direction",
                },
                status_code=200,
            )
        # Opposite direction open -> reverse.
        close_result = client.close_position(signal.symbol)
        logger.info(
            "Reversing %s -> %s on %s: closed %s",
            position["side"], target_side, signal.symbol, close_result,
        )

    # Entry order: resolve qty (auto-size from balance if omitted), TP/SL, leverage.
    price = client.get_price(signal.symbol)
    if price is None:
        raise BybitError(f"Could not fetch current price for {signal.symbol}")

    qty = signal.qty
    if qty is None:
        balance = client.get_balance()
        margin = balance * runtime.margin_percent  # e.g. 90% of wallet balance
        qty = (margin * runtime.leverage) / price
        logger.info(
            "Auto-sized qty %s for %s (margin=$%s = %.0f%% of balance $%s, %sx, price=%s)",
            qty, signal.symbol, margin, runtime.margin_percent * 100,
            balance, leverage, price,
        )

    safety.validate_qty(qty, price, settings)

    tp, sl = client.compute_tp_sl(
        signal, price, tp_percent=runtime.tp_percent, sl_percent=runtime.sl_percent
    )
    safety.validate_tp_sl_side(tp, sl, price, target_side)
    result = client.place_market_order(
        signal.symbol, target_side, qty, leverage=leverage, tp=tp, sl=sl
    )

    logger.info("Entry executed: %s %s %s tp=%s sl=%s -> %s",
                signal.symbol, target_side, qty, tp, sl, result)
    return JSONResponse(
        {
            "accepted": True,
            "action": "entry",
            "symbol": signal.symbol,
            "side": target_side,
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
