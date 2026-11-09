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
(or `WEBHOOK_SECRET` if that is unset). The page reads top-down in four sections — what is
running, what is open, what was scanned, then what to add:

- **Bot now trading** — the settings that apply to every trade the bot opens, live and without
  a redeploy:
  - **Trading on/off** kill switch (same effect as `TRADING_ENABLED`).
  - **Leverage** — any value from 1 up to `MAX_LEVERAGE` (raise that env var to go above 5x).
  - **Margin %** — fraction of wallet balance used per trade (auto-sizing).
  - **Stop-loss distance %** and an optional **take-profit distance %** (0 = no TP).

  A change takes effect on the **next entry**, never on a position that is already open.
- **Open positions** — USDT wallet balance and every open position per allowed pair: side,
  size, entry, live mark price, the **real SL/TP read from Bybit's open stop-orders**
  (TKKBOT attaches these order-level, so they do not appear on the position row), live PnL in
  dollars and as a % of the position margin, plus a per-pair **Close** button (reduce-only).
  The panel auto-refreshes every ~10 seconds; the SL/TP shown here are the exchange's, not the
  ones set above.
- **Market scan — informational only** — the latest scan's Top 10 (5 crypto + 5 tech stocks)
  with price, volatility, direction bias, confidence, suggested entry/stop/target, horizon,
  thesis and key risks; a **NEW** tag on symbols that were not in the previous run; the source
  funnel, which sources failed, the CoinGecko/CMC disagreements, earlier runs today, and when
  the next scan is due. **BTC is pinned first as the market anchor**, so the page normally
  shows eleven rows — if BTC also made the top ten its row carries the rank (`#7`) instead of
  appearing twice. It is fed by `GET /api/dashboard/opportunities` (session cookie, not
  the scanner's `X-API-Key`, so the key never reaches the browser), refreshes every 60 s, and
  is strictly read-only: **nothing on this page can place a trade from a scan.**
- **Your trades** — one row per trade the bot will take, each with its **name**, its saved
  **qty**, and an **Alert** button. **+ Start a new trade** collects the pair, name and qty in
  one step, then shows that trade's copy-paste alert (see *Trading several pairs*).

### Trading several pairs

Auto-sizing commits `MARGIN_USAGE_PERCENT` (90% by default) of the **whole** wallet on every
alert, so two pairs drawing on one balance cannot both open — the second is rejected by Bybit
for insufficient margin. Give each pair its own **qty** instead:

1. Under **Your trades**, click **+ Start a new trade** and fill in the pair (e.g. `ETHUSDT`),
   a **Name** (`eth-swing`) and a **Qty** in base units.
2. The pair, its name and its qty are saved in **one** step, and that trade's alert opens
   straight away: the webhook URL and the exact JSON body to paste into a TradingView alert,
   each with a **Copy** button.
3. Repeat per pair. Every pair keeps its own name and qty, and all of them trade through the
   same bot. To change a name or qty later, edit it in the pair's row and **Save**.

**+ Start a new trade** is for a pair the bot does not have yet. Typing a pair that is already
listed is refused — including the seed pair from `ALLOWED_SYMBOLS`, which is listed before you
have added anything — so name and size it in its existing row instead, and use that row's
**Alert** button for its copy-paste webhook.

Sizing for an entry resolves in this order: the alert's own `qty`, else that pair's saved
qty, else auto-size. The saved qty therefore only applies to an alert that omits `qty` — and
never to `side: "close"`, which always closes the whole position.

The **name** appears in the bot's logs and on the order's `orderLinkId` on Bybit
(`tkk_<name>_<random>`), so a trade is identifiable in both. Names must be 1–20 characters of
`a-z`, `0-9`, `-` or `_`, and no two pairs may share one.

A pair with **no** saved qty is flagged in the dashboard whenever more than one pair is
allowed, because that pair still auto-sizes and can over-commit the wallet.

Changes are saved to a JSON file (see `TKKBOT_CONFIG_PATH`) and take effect on the **next
entry**. They do not move the stop-loss of a position that is already open — send the opposite
signal (which flips: closes and re-opens) to apply new values immediately. Existing positions
are never touched when you remove a pair from the list.

| Field | Required | Notes |
|---|---|---|
| `secret` | yes | Must equal your `WEBHOOK_SECRET` env var |
| `symbol` | yes | `BTCUSDT`, `ETHUSDT`, … (TradingView forms like `BINANCE:BTCUSDT.P` are normalized) |
| `side` | yes | `buy` / `long` / `sell` / `short` / `close` / `exit` |
| `qty` | no | Base-asset size. Omitted → the pair's saved qty, else auto-sized from `MARGIN_USAGE_PERCENT` |
| `leverage` | no | **Ignored** — the dashboard sets leverage. Accepted so existing alerts keep working |
| `tp` / `sl` | no | Prices. Omitted → computed from `DEFAULT_TP_PERCENT` / `DEFAULT_SL_PERCENT` |
| `id` | no | Idempotency key for duplicate suppression (recommended) |

Aliases accepted: `action`/`direction` for side, `quantity`/`contracts` for qty,
`takeProfit`/`stopLoss`/`targetPrice`/`stopPrice` for tp/sl.

## Market scanner (informational — never trades)

A self-contained `scanner/` package ranks **5 cryptocurrencies + 5 tech stocks four times a
day** and stores the result. It sits deliberately outside the trading path: nothing in
`scanner/` signs a Bybit order or touches the webhook, and `OPPORTUNITIES_FEED_TO_BOT` is a
documented stub that does nothing.

### How a scan works

1. **Universe** — top ~250 coins by market cap from CoinGecko, filtered to
   `MIN_CRYPTO_MCAP_USD` / `MIN_CRYPTO_VOLUME_USD` and stripped of stablecoins plus wrapped
   and staked derivatives. Stocks come from `scanner/tech_universe.json` (plus any top movers
   the provider returns) above `MIN_STOCK_AVG_VOLUME`.
2. **Metrics** — 24h volatility `((high − low) / low) × 100`, volume spike, momentum,
   distance from the 24h high/low, ATR(14) from Bybit daily candles, and order-book depth.
3. **Pre-score in Python** — a deterministic weighted composite (default 40% volatility, 25%
   volume spike, 20% momentum, 15% liquidity) read from `scanner/scoring_weights.json`.
4. **One DeepSeek call** — the top 15 of each side, strict JSON, validated against the data:
   unknown tickers, wrong counts and contradictory prices/volatility are rejected. One
   corrective retry, then a deterministic fallback flagged `llm_fallback: true`.
   The call also returns an **anchor**: the model's own view of **BTC**, which is added to the
   candidates even when its low volatility keeps it out of the shortlist. A missing or
   contradicting anchor is replaced by the computed figures rather than costing a retry, so
   every stored scan carries one.
5. **Store + notify** — every scan is kept (SQLite, or JSON files) and optionally sent to
   Telegram with NEW markers and a "not financial advice" footer.

US market hours are respected: outside Mon–Fri 09:30–16:00 New York time the stock rows are
flagged stale, but crypto is still scanned. Every provider has a timeout, retries with
exponential backoff, 429/`Retry-After` handling and per-run caching; a dead source is recorded
in `sources_failed` and the scan carries on.

> **Investing.com is not used.** It has no official public API, and scraping it violates its
> terms and breaks behind Cloudflare. `scanner/providers/investingcom.py` is a disabled stub
> that explains this; the equivalent data comes from Twelve Data / Finnhub / Yahoo.

### Endpoints

All three need `X-API-Key: $OPPORTUNITIES_API_KEY`; when that variable is empty they return
503 rather than being open.

| Route | Purpose |
|---|---|
| `GET /opportunities/latest` | the most recent stored scan (404 before the first) |
| `GET /opportunities/history?days=7` | stored scans inside the window |
| `POST /opportunities/scan` | start a scan now; returns **202** immediately (a run takes minutes), poll `/latest` for the result |

The same data is on the dashboard, under **Market scan — informational only**, via the
session-authenticated `GET /api/dashboard/opportunities` — that route exists so the browser
never has to hold `OPPORTUNITIES_API_KEY`. It is read-only; it cannot start a scan.

```bash
curl -H "X-API-Key: $OPPORTUNITIES_API_KEY" \
  https://<project>.up.railway.app/opportunities/latest
```

### Running it

```bash
python -m scanner.run --dry-run --no-telegram   # sample data; no keys, no network
python -m scanner.run --no-telegram             # one live scan, printed summary
python -m scanner.run --json                    # ...as JSON
```

`POST /opportunities/scan` runs the same thing inside the web service and answers 202
straight away: a full scan outlives Railway's ~60-second HTTP timeout (Twelve Data's free
tier is paced at one request a minute), so a synchronous call would always be disconnected.
Poll `/opportunities/latest` instead. A second trigger while a scan is running is ignored
and logged — the scan lock is the authority, not the endpoint.

`--dry-run` uses `scanner/sample_data/` and never calls a provider or the LLM; use it to check
scoring and message formatting before adding keys.

### Railway

Preferred: a **separate cron service** in the same project, built from the same repo, with
command `python -m scanner.run`, a Railway cron schedule of `0 0,6,12,18 * * *` (UTC), its own
`/data` volume mounted at `/data`, and the scanner variables from `.env.example`. The cron
service does not need the Bybit keys.

Alternative: set `SCAN_SCHEDULER_ENABLED=true` on the web service. It starts a lightweight
thread firing at `SCAN_TIMES_UTC`; it is off by default because a redeploy restarts the clock
and a long sleep is easy to lose. Do not run both — the scan lock makes the second caller back
off, but it still costs a call.

Required variables: `DEEPSEEK_API_KEY`, `COINGECKO_API_KEY`, `CMC_API_KEY`, and either
`TWELVEDATA_API_KEY` or `FINNHUB_API_KEY`. A missing key fails fast with a message naming the
variable; key *values* are never logged (the startup line prints `on`/`off` only).

## Repository layout

```
app/
  main.py             FastAPI app, /webhook/tradingview endpoint, /health, /status
  config.py           All env-var settings (pydantic-settings)
  signals.py          Parse & normalize TradingView payloads
  safety.py           Guardrails + duplicate-alert cooldown
  bybit_client.py     Bybit V5 REST wrapper (pybit)
  runtime_config.py   Dashboard-tunable settings, persisted to a file (Railway volume)
  dashboard.py        /dashboard page + /api/dashboard/* (login, state, config, pairs,
                      close, alert-template, opportunities)
  static/dashboard.html  Self-contained dashboard front-end (no build step)
scanner/              Informational market scanner: providers, scoring, LLM, storage, API
tests/                pytest suite (365 tests)
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
  (the dashboard's **Alert** button on each pair prints this, so you can copy it)
- **Message** — paste one of these templates:

**Simple alert (static symbol):**
```json
{
  "secret": "YOUR_WEBHOOK_SECRET",
  "symbol": "BTCUSDT",
  "side": "buy",
  "qty": 0.001,
  "tp": 72000,
  "sl": 66000
}
```
Omit `qty` to use that pair's saved quantity (or auto-size if it has none). `leverage` is
not listed because the dashboard sets it — sending it changes nothing.

**Strategy alert (placeholders resolved at fire time):**
```json
{
  "secret": "YOUR_WEBHOOK_SECRET",
  "symbol": "{{ticker}}",
  "side": "{{strategy.order.action}}",
  "qty": {{strategy.order.contracts}},
  "price": {{close}},
  "id": "{{ticker}}_{{strategy.order.action}}_{{time}}"
}
```

- `{{strategy.order.action}}` → `buy` / `sell`; `{{ticker}}` → e.g. `BTCUSDT` or `BINANCE:BTCUSDT.P`.
- Include `{{time}}` (or any per-signal value) in `id`: a *static* `id` makes every alert on
  that pair share one cooldown key, so a buy→sell flip inside `COOLDOWN_SECONDS` is suppressed.
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

Run tests: `pytest -q` (365 tests, no real API calls).

## 6. Going live — checklist

1. Confirm the bot trades correctly on **testnet** (`BYBIT_TESTNET=true`, testnet keys, real TradingView alerts).
2. Confirm TP/SL orders appear attached on the testnet position.
3. Set `BYBIT_TESTNET=false`, swap in the **live** key, and keep `TRADING_ENABLED=false` for a first deploy.
4. Confirm alerts are *accepted but not traded* (logs show the signals; no orders placed).
5. Flip `TRADING_ENABLED=true` only when you're ready to risk real funds.

## Safety notes

- **One-way position mode** is assumed (`positionIdx: 0`). If your Bybit account is in hedge mode, disable hedge mode in Bybit → Trading Account.
- Alert bodies are **never logged verbatim**: the `secret` field is masked to `***` before anything reaches the log (see `signals.redact_secret`), because the webhook secret is the credential that authorises live trades. Rotate `WEBHOOK_SECRET` and update your TradingView alerts if it has ever been pasted somewhere shared.
- Qty is rounded down to the symbol's lot step before ordering (e.g. `0.00714` → `0.007` for BTCUSDT).
- The bot never withdraws, never transfers, and only ever places market orders on allowed symbols.
- `close` is `reduceOnly` — it can only reduce, never flip, a position.

## Disclaimer

Trading perpetual futures is high risk. You can lose more than you deposit. TKKBOT ships with
guardrails, but they are not a guarantee against loss — test thoroughly on testnet first.
