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
from scanner.api import create_opportunities_router
from scanner.scheduler import start_if_enabled

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
    # Market scanner: read-only, informational, and entirely outside the
    # trading path. Keyed separately (X-API-Key) from the dashboard.
    app.include_router(create_opportunities_router())
    # No-op unless SCAN_SCHEDULER_ENABLED=true; a Railway cron is preferred.
    start_if_enabled()

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
            logger.warning("Bad payload: %s", signals.redact_secret(e.message, settings.webhook_secret))
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
    # Never log the payload verbatim: it carries the webhook secret, which is the
    # key that authorises live trades.
    logger.info("Webhook received: %s", signals.redact_secret(raw, settings.webhook_secret))

    data = signals.parse_payload(raw)
    signal = signals.build_signal(data, settings)

    safety.check_configuration(settings)
    safety.validate_secret(signal.secret, settings)
    safety.validate_trading_enabled(runtime)
    safety.validate_symbol(signal.symbol, runtime)

    # The pair's short name: identifies this pair's orders on Bybit and in the
    # logs. Read once, so a dashboard edit mid-alert cannot split one trade's
    # two legs across two different labels.
    label = runtime.pair_label(signal.symbol)

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
        # A close always means flat. The per-pair saved quantity must NOT apply
        # here: it would turn a full close into a partial one and strand the
        # remainder of the position.
        result = client.close_position(signal.symbol, qty=signal.qty, label=label)
        logger.info("Close executed: %s label=%s -> %s", signal.symbol, label, result)
        return JSONResponse(
            {
                "accepted": True,
                "action": "close",
                "symbol": signal.symbol,
                "label": label,
                "result": result,
            },
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
                    "label": label,
                    "side": position["side"],
                    "reason": "already in this direction",
                },
                status_code=200,
            )

    # Everything the entry needs is resolved and validated *before* the reversal
    # close below. The close used to happen first, so any failure afterwards —
    # a quantity over a cap, an inverted stop-loss, a quantity below the lot
    # step — left the bot FLAT, breaking the always-in-the-market guarantee.
    # A bad alert is now refused with the existing position still open.
    price = client.get_price(signal.symbol)
    if price is None:
        raise BybitError(f"Could not fetch current price for {signal.symbol}")

    qty, qty_source = _resolve_qty(signal, runtime, client, price)

    # Validate the rounded quantity — the number that will actually be sent — so
    # approval here cannot be undone by place_market_order's own rounding.
    qty = client.round_qty(signal.symbol, qty)
    if qty <= 0:
        raise SafetyError(
            f"Qty for {signal.symbol} is below the minimum lot size "
            f"({client.get_qty_step(signal.symbol)})",
            400,
        )
    try:
        safety.validate_qty(qty, price, settings)
    except SafetyError as e:
        # Say where the number came from. An alert that omits qty is sized from
        # the pair's saved setting, and the user has to know that to fix it.
        hint = _QTY_HINTS.get(qty_source)
        if hint is None:
            raise
        raise SafetyError(f"{e.message} (qty {qty} came from {hint})", e.status_code) from e

    tp, sl = client.compute_tp_sl(
        signal, price, tp_percent=runtime.tp_percent, sl_percent=runtime.sl_percent
    )
    safety.validate_tp_sl_side(tp, sl, price, target_side)

    # Applying leverage is itself an exchange call, and it can fail — a symbol
    # whose own maximum sits below the dashboard's setting, for instance. It is
    # done here, and not inside place_market_order, so that failure also lands
    # before the reversal close rather than after it. Bybit treats re-applying
    # the current leverage as success, so this is safe to repeat.
    client.set_leverage(signal.symbol, leverage)

    if position is not None:
        close_result = client.close_position(signal.symbol, label=label)
        logger.info(
            "Reversing %s -> %s on %s: closed %s",
            position["side"], target_side, signal.symbol, close_result,
        )

    # leverage=None: already applied above.
    result = client.place_market_order(
        signal.symbol, target_side, qty, leverage=None, tp=tp, sl=sl, label=label
    )

    logger.info("Entry executed: %s %s %s tp=%s sl=%s label=%s qty_source=%s -> %s",
                signal.symbol, target_side, qty, tp, sl, label, qty_source, result)
    return JSONResponse(
        {
            "accepted": True,
            "action": "entry",
            "symbol": signal.symbol,
            "label": label,
            "side": target_side,
            "qty": qty,
            "qty_source": qty_source,
            "leverage": leverage,
            "tp": tp,
            "sl": sl,
            "result": result,
        },
        status_code=200,
    )


# Appended to a guardrail failure so the user can tell an alert's own qty from
# the one this pair remembers.
_QTY_HINTS = {
    "pair_saved": "this pair's saved quantity — change it in the dashboard",
    "auto": "auto-sizing from the wallet balance",
}


def _resolve_qty(
    signal: signals.TradeSignal, runtime: RuntimeConfig, client: BybitClient, price: float
) -> tuple[float, str]:
    """Resolve the entry quantity, and say where it came from.

    Precedence: the alert's own ``qty``, else the pair's saved quantity, else
    auto-size from the wallet. The middle step is what makes several pairs
    possible at once — auto-size commits ``margin_percent`` of the *whole*
    wallet on every alert, so a second pair drawing on the same balance is
    rejected by Bybit for insufficient margin.
    """
    if signal.qty is not None:
        return signal.qty, "alert"

    saved = runtime.pair_qty(signal.symbol)
    if saved is not None:
        logger.info("No qty in alert; using the saved qty %s for %s", saved, signal.symbol)
        return saved, "pair_saved"

    balance = client.get_balance()
    margin = balance * runtime.margin_percent  # e.g. 90% of wallet balance
    qty = (margin * runtime.leverage) / price
    logger.info(
        "Auto-sized qty %s for %s (margin=$%s = %.0f%% of balance $%s, %sx, price=%s)",
        qty, signal.symbol, margin, runtime.margin_percent * 100,
        balance, runtime.leverage, price,
    )
    return qty, "auto"


settings = get_settings()
app = create_app(settings)
