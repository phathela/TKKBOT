# TKKBOT

A Python **FastAPI** bot that listens for **TradingView alert webhooks** and trades **USDT perpetual futures on Bybit**. Designed to run on **Railway** with code hosted on GitHub.

```
TradingView alert (HTTP POST)  ──►  https://<project>.up.railway.app/webhook/tradingview
                                          │ 1. secret check           (bad secret  → 401)
                                          ▼
                                   parse + validate payload          (bad shape   → 400)
                                          │ 2. safety guardrails      (over-limit  → 400)
                                          │    - symbol allowlist
                                          │    - qty / notional caps
                                          │    - leverage cap
                                          │    - TRADING_ENABLED kill switch
                                          │    - duplicate-alert cooldown
                                          ▼
                                   Bybit V5 REST API
                                          │ POST /v5/order/create   (market, linear)
                                          │ POST /v5/position/set-leverage
                                          ▼
                                   200 OK + order result  (logged)
```

## What an alert does

Each alert carries a JSON message. **The bot is always in the market**: a `side: "buy"`
makes it long, a `side: "sell"` makes it short, flipping automatically when the direction
changes — if an opposite position is open it is closed (reduce-only) and a fresh position is
opened on the alert's side. A `side: "close"` closes whatever is open and goes flat. An alert
that matches the current direction is a hold (never doubles up). TP/SL are attached
**on the exchange** (not tracked in the bot), so they survive restarts and disconnects.

> **The dashboard is the source of truth for sizing:** leverage, margin %, stop-loss %,
> take-profit % and the allowed pair list are set in the web dashboard (`/dashboard`) and are
> persisted, so an alert's own `leverage` field is **ignored** (it used to win; now it would
> silently defeat the dashboard). An alert may still override `qty` and send explicit
> `tp`/`sl` **prices**, which are honoured per-trade. Defaults on first boot come from the env
> vars below (BTCUSDT, 5x, 90% margin, 4% SL).

## Dashboard (web control panel)

Open `https://<project>.up.railway.app/dashboard` and sign in with `DASHBOARD_PASSWORD`
(or `WEBHOOK_SECRET` if that is unset). From there you can, live, without redeploying:

- **Trading on/off** kill switch (same effect as `TRADING_ENABLED`).
- **Leverage** — any value from 1 up to `MAX_LEVERAGE` (raise that env var to go above 5x).
- **Margin %** — fraction of wallet balance used per trade (auto-sizing).
- **Stop-loss distance %** and an optional **take-profit distance %** (0 = no TP).
- **Allowed pairs** — search the real Bybit perpetual list (e.g. `ETHUSDT`, `1000PEPEUSDT`)
  and add/remove them. Alerts trade whichever allowed pair they name.
- **Live status** — USDT wallet balance and every open position per allowed pair, with its
  live exchange SL/TP, plus a per-pair **Close** button (reduce-only).

Changes are saved to a JSON file (see `TKKBOT_CONFIG_PATH`) and take effect on the **next
entry**. They do not move the stop-loss of a position that is already open — send the opposite
signal (which flips: closes and re-opens) to apply new values immediately. Existing positions
are never touched when you remove a pair from the list.

| Field | Required | Notes |
|---|---|---|
| `secret` | yes | Must equal your `WEBHOOK_SECRET` env var |
| `symbol` | yes | `BTCUSDT`, `ETHUSDT`, … (TradingView forms like `BINANCE:BTCUSDT.P` are normalized) |
| `side` | yes | `buy` / `long` / `sell` / `short` / `close` / `exit` |
| `qty` | no | Base-asset size. Omitted → auto-sized from `MARGIN_USD_PER_TRADE` |
| `leverage` | no | Defaults to `DEFAULT_LEVERAGE`; capped at `MAX_LEVERAGE` |
| `tp` / `sl` | no | Prices. Omitted → computed from `DEFAULT_TP_PERCENT` / `DEFAULT_SL_PERCENT` |
| `id` | no | Idempotency key for duplicate suppression (recommended) |

Aliases accepted: `action`/`direction` for side, `quantity`/`contracts` for qty,
`takeProfit`/`stopLoss`/`targetPrice`/`stopPrice` for tp/sl.

## Repository layout

```
app/
  main.py             FastAPI app, /webhook/tradingview endpoint, /health, /status
  config.py           All env-var settings (pydantic-settings)
  signals.py          Parse & normalize TradingView payloads
  safety.py           Guardrails + duplicate-alert cooldown
  bybit_client.py     Bybit V5 REST wrapper (pybit)
  runtime_config.py   Dashboard-tunable settings, persisted to a file (Railway volume)
  dashboard.py        /dashboard page + /api/dashboard/* (login, state, config, pairs, close)
  static/dashboard.html  Self-contained dashboard front-end (no build step)
tests/                pytest suite (116 tests)
Procfile           web: uvicorn app.main:app --port $PORT
runtime.txt        Python 3.12
railway.json       Railway deploy config (healthcheck on /health)
.env.example       Documented env vars
```

## 1. Bybit API key (do this carefully)

1. Log in to [Bybit](https://www.bybit.com) → **API** → **Create new key**.
2. System: **API V3**, type: **System-generated**.
3. Permissions: **Read-Write**, enable **Contract Trading** (covers USDT perps / LINEAR). 
4. **DO NOT enable withdrawal.**
5. **DO NOT enable IP whitelist** — Railway uses dynamic egress IPs; a whitelist will randomly break the bot.
6. Save the key + secret. For testing, create a *second* key on the [Bybit testnet portal](https://testnet.bybit.com) (separate login; testnet funds are free).

> ⚠️ The live key trades **real money**. Start with the testnet key + `BYBIT_TESTNET=true`.

## 2. Deploy to Railway

1. Push this repo to GitHub (or `gh repo create TKKBOT --source . --push`).
2. In [Railway](https://railway.app): **New Project → Deploy from GitHub repo → `TKKBOT`**.
   Railway auto-detects Python (`requirements.txt`), honors the `Procfile`, and injects `PORT`.
3. **Variables** tab — set at minimum:
   - `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `WEBHOOK_SECRET`
   - `BYBIT_TESTNET=true` for a first test run
4. **Networking → Generate Domain** → gives you `https://<project>.up.railway.app`.
5. Check the deploy: `https://<project>.up.railway.app/health` should return `{"status":"ok",...}`.
6. Railway redeploys automatically on every push to `main`.

## 3. TradingView alert

Create an alert on a chart (or a strategy's order event). In the alert settings:

- **Conditions/Alert**: whatever triggers your signal.
- **Notifications → Webhook URL**: `https://<project>.up.railway.app/webhook/tradingview`
- **Message** — paste one of these templates:

**Simple alert (static symbol):**
```json
{
  "secret": "YOUR_WEBHOOK_SECRET",
  "symbol": "BTCUSDT",
  "side": "buy",
  "qty": 0.001,
  "leverage": 5,
  "tp": 72000,
  "sl": 66000
}
```

**Strategy alert (placeholders resolved at fire time):**
```json
{
  "secret": "YOUR_WEBHOOK_SECRET",
  "symbol": "{{ticker}}",
  "side": "{{strategy.order.action}}",
  "qty": {{strategy.order.contracts}},
  "leverage": 5,
  "price": {{close}},
  "id": "{{ticker}}_{{strategy.order.action}}_{{time}}"
}
```

- `{{strategy.order.action}}` → `buy` / `sell`; `{{ticker}}` → e.g. `BTCUSDT` or `BINANCE:BTCUSDT.P`.
- `"id"` makes each trigger unique but stable across TradingView's automatic retries, so a duplicate can never double-trade.
- Set alert frequency to **Once Per Bar Close** to avoid intra-bar duplicate triggers.
- **Never** put your Bybit API key in the alert message — only the webhook secret.

### Behavior on responses

- `2xx` → TradingView stops. 
- `4xx` (bad secret, malformed, over-limit) → TradingView does **not** retry.
- `5xx` (e.g. Bybit rejected the order) → TradingView retries up to 3× after ~5s. The cooldown
  is set **before** the order call, so those retries are swallowed — no double trades.

## 4. Environment variables

| Variable | Default | Description |
|---|---|---|
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | — | Bybit V3 key (Contract Trading, no withdrawal, no IP whitelist) |
| `BYBIT_TESTNET` | `false` | `true` = testnet keys + testnet API |
| `WEBHOOK_SECRET` | — | Required; must match `"secret"` in every alert |
| `DASHBOARD_PASSWORD` | *(none)* | Password for `/dashboard`. Empty → `WEBHOOK_SECRET` is used |
| `TKKBOT_CONFIG_PATH` | *(none)* | Where dashboard settings are saved (e.g. `/data/tkkbot_config.json` on a Railway volume). Empty → in-memory only (reset on restart) |
| `TRADING_ENABLED` | `true` | Seed for the dashboard on/off switch (also the env kill switch on first boot) |
| `ALLOWED_SYMBOLS` | `BTCUSDT` | Seed pair list; the dashboard can add more from the Bybit perpetual list |
| `MAX_QTY_PER_ORDER` | `100.0` | Hard cap: reject larger quantities |
| `MAX_NOTIONAL_USD` | `100000` | Hard cap: reject orders above this notional (qty × live price) |
| `MAX_LEVERAGE` | `5` | Ceiling the dashboard allows for leverage (raise to `100` to go above 5x) |
| `MARGIN_USAGE_PERCENT` | `0.90` | Seed for dashboard margin % (auto-size uses this fraction of wallet per trade) |
| `DEFAULT_LEVERAGE` | `5` | Seed for dashboard leverage on first boot |
| `DEFAULT_TP_PERCENT` / `DEFAULT_SL_PERCENT` | `0` / `0.04` | Seeds for dashboard TP/SL distances; `0.04` = 4% price move = 20% of margin at 5x |
| `COOLDOWN_SECONDS` | `5` | Duplicate-alert suppression window |
| `LOG_LEVEL` | `INFO` | `DEBUG` for more detail |
| `PORT` | `8000` | Railway injects this automatically |

## 5. Local development

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (POSIX: source .venv/bin/activate)
pip install -r requirements.txt
cp .env.example .env            # fill in testnet keys + a webhook secret
uvicorn app.main:app --reload
```

Smoke-test with any HTTP client:

```bash
curl -X POST http://localhost:8000/webhook/tradingview ^
  -H "Content-Type: application/json" ^
  -d "{\"secret\":\"YOUR_SECRET\",\"symbol\":\"BTCUSDT\",\"side\":\"buy\",\"qty\":0.001,\"leverage\":5}"
```

Run tests: `pytest -q` (116 tests, no real API calls).

## 6. Going live — checklist

1. Confirm the bot trades correctly on **testnet** (`BYBIT_TESTNET=true`, testnet keys, real TradingView alerts).
2. Confirm TP/SL orders appear attached on the testnet position.
3. Set `BYBIT_TESTNET=false`, swap in the **live** key, and keep `TRADING_ENABLED=false` for a first deploy.
4. Confirm alerts are *accepted but not traded* (logs show the signals; no orders placed).
5. Flip `TRADING_ENABLED=true` only when you're ready to risk real funds.

## Safety notes

- **One-way position mode** is assumed (`positionIdx: 0`). If your Bybit account is in hedge mode, disable hedge mode in Bybit → Trading Account.
- Qty is rounded down to the symbol's lot step before ordering (e.g. `0.00714` → `0.007` for BTCUSDT).
- The bot never withdraws, never transfers, and only ever places market orders on allowed symbols.
- `close` is `reduceOnly` — it can only reduce, never flip, a position.

## Disclaimer

Trading perpetual futures is high risk. You can lose more than you deposit. TKKBOT ships with
guardrails, but they are not a guarantee against loss — test thoroughly on testnet first.
