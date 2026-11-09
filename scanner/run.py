"""Scan orchestration.

``python -m scanner.run`` is the Railway cron entry point; the same
:class:`Scanner` is what ``POST /opportunities/scan`` calls. Nothing here talks
to Bybit with credentials or places orders — the trading path is untouched.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import llm, metrics, notifier, scoring, universe
from .config import ConfigError, ScanSettings, get_scan_settings, log_settings_summary
from .lock import LockBusy, ScanLock
from .providers import (
    BybitProvider,
    CoinGeckoProvider,
    CoinMarketCapProvider,
    FearGreedProvider,
    FinnhubProvider,
    HttpClient,
    ProviderError,
    TwelveDataProvider,
    YahooProvider,
)
from .providers.cryptopanic import CryptoPanicProvider
from .providers.newsapi import NewsApiProvider
from .schemas import (
    Candidate,
    CryptoMarket,
    Headline,
    ScanResult,
    SourceStatus,
    StockQuote,
)
from .storage import ScanStore, open_store

logger = logging.getLogger(__name__)

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_data"
US_EASTERN = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


class Scanner:
    def __init__(
        self,
        settings: ScanSettings | None = None,
        *,
        http: HttpClient | None = None,
        store: ScanStore | None = None,
    ) -> None:
        self.settings = settings or get_scan_settings()
        self._http = http
        self._store = store

    # -- public ------------------------------------------------------------

    def run(self, *, dry_run: bool = False, notify: bool = True) -> ScanResult:
        self.settings.ensure_ready(dry_run=dry_run)
        log_settings_summary(self.settings)
        data_dir = self.settings.data_dir()
        store = self._store or open_store(self.settings.db_path(), data_dir)
        lock = ScanLock(data_dir / "scan.lock")

        with lock:
            result = self._dry_run() if dry_run else self._live_run()
            previous = store.recent_symbols(scans=1)
            result.new_symbols = [
                item.symbol for item in result.items if item.symbol not in previous
            ]
            if dry_run:
                logger.info("Dry run: result not stored or delivered")
                return result
            _safe_store(store, result)
            if notify:
                text = notifier.build_message(result, previous)
                notifier.send_message(
                    text,
                    token=self.settings.telegram_bot_token,
                    chat_id=self.settings.telegram_chat_id,
                    http=self._client(),
                )
            return result

    # -- live pipeline -----------------------------------------------------

    def _live_run(self) -> ScanResult:
        now = datetime.now(timezone.utc)
        market_open = is_us_market_open(now)
        failed: list[SourceStatus] = []
        used: list[str] = []
        discrepancies: list[str] = []

        crypto_markets = self._fetch_crypto(failed, used, discrepancies)
        stock_quotes = self._fetch_stocks(failed, used, market_open)
        fear_greed = self._fetch_fear_greed(failed, used)

        crypto_selected = universe.select_crypto(
            crypto_markets,
            min_market_cap_usd=self.settings.min_crypto_mcap_usd,
            min_volume_usd=self.settings.min_crypto_volume_usd,
        )
        stock_selected = universe.select_stocks(
            stock_quotes,
            min_average_volume=self.settings.min_stock_avg_volume,
            watchlist=universe.load_tech_universe(self.settings.tech_universe_file()),
        )

        crypto_candidates = scoring.build_crypto_candidates(crypto_selected)
        self._enrich_with_bybit(crypto_candidates, failed, used)
        stock_candidates = scoring.build_stock_candidates(stock_selected)

        weights = scoring.load_weights(self.settings.scoring_weights_file())
        crypto_ranked = scoring.score_candidates(
            crypto_candidates, weights
        )[: self.settings.crypto_candidate_limit]
        stock_ranked = scoring.score_candidates(
            stock_candidates, weights
        )[: self.settings.stock_candidate_limit]

        shortlist: list[Candidate] = sorted(
            crypto_ranked + stock_ranked, key=lambda c: (-c.score, c.symbol)
        )
        headlines = self._headlines(shortlist, failed, used)

        # A dead source must not stop the scan: ask for as many items as there
        # are candidates, so a crypto-only or stock-only run still completes.
        n_crypto = min(self.settings.final_crypto, len(crypto_ranked))
        n_stocks = min(self.settings.final_stocks, len(stock_ranked))
        analysis = llm.analyse(
            shortlist=shortlist,
            headlines=headlines,
            client=llm.DeepSeekClient(
                self._client(),
                api_key=self.settings.deepseek_api_key,
                model=self.settings.deepseek_model,
                base_url=self.settings.deepseek_base_url,
            ),
            n_crypto=n_crypto,
            n_stocks=n_stocks,
            market_open=market_open,
            fear_greed=fear_greed,
        )

        return ScanResult(
            run_id=uuid.uuid4().hex,
            created_at=ScanResult.now_iso(),
            market_open=market_open,
            llm_fallback=analysis.fallback,
            llm_error=analysis.error,
            items=analysis.items,
            sources_used=used,
            sources_failed=failed,
            discrepancies=discrepancies,
            candidates_considered={
                "crypto_markets": len(crypto_markets),
                "crypto_selected": len(crypto_selected),
                "stocks_quoted": len(stock_quotes),
                "stocks_selected": len(stock_selected),
                "shortlist": len(shortlist),
            },
        )

    # -- sources -----------------------------------------------------------

    def _fetch_crypto(
        self,
        failed: list[SourceStatus],
        used: list[str],
        discrepancies: list[str],
    ) -> list[CryptoMarket]:
        markets: list[CryptoMarket] = []
        try:
            markets = CoinGeckoProvider(
                self._client(), self.settings.coingecko_api_key
            ).markets(limit=self.settings.crypto_universe_limit)
            used.append("coingecko")
        except ProviderError as exc:
            failed.append(SourceStatus(name="coingecko", ok=False, detail=str(exc)))
            logger.error("CoinGecko unavailable: %s", exc)

        if self.settings.cmc_api_key:
            try:
                cmc = CoinMarketCapProvider(self._client(), self.settings.cmc_api_key)
                listings = cmc.listings(limit=self.settings.crypto_universe_limit)
                used.append("coinmarketcap")
                discrepancies.extend(cmc.cross_check(markets, listings))
            except ProviderError as exc:
                failed.append(
                    SourceStatus(name="coinmarketcap", ok=False, detail=str(exc))
                )
                logger.warning("CoinMarketCap unavailable: %s", exc)
        return markets

    def _fetch_stocks(
        self,
        failed: list[SourceStatus],
        used: list[str],
        market_open: bool,
    ) -> dict[str, StockQuote]:
        quotes: dict[str, StockQuote] = {}
        symbols = universe.load_tech_universe(self.settings.tech_universe_file())

        if self.settings.twelvedata_api_key:
            try:
                provider = TwelveDataProvider(
                    self._client(),
                    self.settings.twelvedata_api_key,
                    chunk_size=self.settings.twelvedata_chunk_size,
                    chunk_delay_seconds=self.settings.twelvedata_chunk_delay_seconds,
                )
                quotes = provider.quotes(symbols)
                used.append("twelvedata")
            except ProviderError as exc:
                failed.append(SourceStatus(name="twelvedata", ok=False, detail=str(exc)))
                logger.error("Twelve Data unavailable: %s", exc)

        if not quotes and self.settings.finnhub_api_key:
            try:
                finnhub = FinnhubProvider(
                    self._client(),
                    self.settings.finnhub_api_key,
                    request_delay_seconds=self.settings.finnhub_request_delay_seconds,
                )
                quotes = finnhub.quotes(symbols)
                averages = finnhub.average_volumes(list(quotes))
                for symbol, average in averages.items():
                    if symbol in quotes:
                        quotes[symbol].avg_volume = average
                used.append("finnhub")
            except ProviderError as exc:
                failed.append(SourceStatus(name="finnhub", ok=False, detail=str(exc)))
                logger.error("Finnhub unavailable: %s", exc)

        missing = [s for s in symbols if s not in quotes]
        if missing:
            yahoo = YahooProvider(self._client())
            if yahoo.available():
                fallback = yahoo.quotes(missing)
                if fallback:
                    quotes.update(fallback)
                    used.append("yahoo")
            elif not quotes:
                failed.append(
                    SourceStatus(
                        name="yahoo", ok=False,
                        detail="yfinance not installed and no primary provider worked",
                    )
                )

        if not market_open:
            for quote in quotes.values():
                quote.stale = True

        if not quotes:
            logger.warning("No stock quotes available; the scan will rank crypto only")
        return quotes

    def _fetch_fear_greed(
        self, failed: list[SourceStatus], used: list[str]
    ) -> dict | None:
        try:
            value = FearGreedProvider(self._client()).current()
            used.append("fear_greed")
            return value
        except ProviderError as exc:
            failed.append(SourceStatus(name="fear_greed", ok=False, detail=str(exc)))
            logger.info("Fear & Greed unavailable: %s", exc)
            return None

    def _headlines(
        self,
        shortlist: list[Candidate],
        failed: list[SourceStatus],
        used: list[str],
    ) -> list[Headline]:
        headlines: list[Headline] = []
        if self.settings.cryptopanic_api_key:
            try:
                headlines.extend(
                    CryptoPanicProvider(
                        self._client(), self.settings.cryptopanic_api_key
                    ).headlines(limit=6)
                )
                used.append("cryptopanic")
            except ProviderError as exc:
                failed.append(SourceStatus(name="cryptopanic", ok=False, detail=str(exc)))

        if self.settings.finnhub_api_key:
            stock_symbols = [c.symbol for c in shortlist if c.asset_type == "stock"][:5]
            if stock_symbols:
                try:
                    headlines.extend(
                        FinnhubProvider(
                            self._client(), self.settings.finnhub_api_key
                        ).company_headlines(stock_symbols)
                    )
                    if "finnhub" not in used:
                        used.append("finnhub")
                except ProviderError as exc:
                    failed.append(
                        SourceStatus(name="finnhub_news", ok=False, detail=str(exc))
                    )

        if self.settings.news_api_key and len(headlines) < 8:
            try:
                headlines.extend(
                    NewsApiProvider(self._client(), self.settings.news_api_key)
                    .headlines(limit=5)
                )
                used.append("newsapi")
            except ProviderError as exc:
                failed.append(SourceStatus(name="newsapi", ok=False, detail=str(exc)))
        return headlines[:12]

    def _enrich_with_bybit(
        self,
        candidates: list[Candidate],
        failed: list[SourceStatus],
        used: list[str],
    ) -> None:
        """Replace CoinGecko's day stats with Bybit's, and fill ATR/spike."""
        if not candidates:
            return
        bybit = BybitProvider(self._client())
        try:
            tickers = bybit.tickers()
        except ProviderError as exc:
            failed.append(SourceStatus(name="bybit", ok=False, detail=str(exc)))
            logger.warning("Bybit tickers unavailable: %s", exc)
            return
        used.append("bybit")

        enriched = sorted(candidates, key=lambda c: -(c.liquidity_usd or 0))[
            : self.settings.crypto_enrich_limit
        ]
        for candidate in enriched:
            pair = f"{candidate.symbol}USDT"
            row = tickers.get(pair)
            if row:
                _apply_bybit_ticker(candidate, row)
            try:
                candles = bybit.klines(pair, limit=20)
            except ProviderError as exc:
                logger.info("Bybit candles unavailable for %s: %s", pair, exc)
                continue
            if not candles:
                continue
            highs = [_f(row[2]) for row in candles]
            lows = [_f(row[3]) for row in candles]
            closes = [_f(row[4]) for row in candles]
            atr_value = metrics.atr(highs, lows, closes)
            if atr_value is not None:
                candidate.atr14 = round(atr_value, 8)
            turnovers = [_f(row[6]) for row in candles]
            if len(turnovers) >= 8 and turnovers[-1] and all(turnovers[-8:-1]):
                historical = sum(turnovers[-8:-1]) / 7
                if historical > 0:
                    candidate.volume_spike = round(turnovers[-1] / historical, 4)

    # -- dry run -----------------------------------------------------------

    def _dry_run(self) -> ScanResult:
        markets = [
            CryptoMarket(**row)
            for row in _load_fixture("crypto_markets.json")
        ]
        quotes = {
            row["symbol"]: StockQuote(**row)
            for row in _load_fixture("stock_quotes.json")
        }
        crypto_selected = universe.select_crypto(
            markets,
            min_market_cap_usd=self.settings.min_crypto_mcap_usd,
            min_volume_usd=self.settings.min_crypto_volume_usd,
        )
        stock_selected = universe.select_stocks(
            quotes, min_average_volume=self.settings.min_stock_avg_volume
        )
        weights = scoring.load_weights(self.settings.scoring_weights_file())
        crypto_ranked = scoring.score_candidates(
            scoring.build_crypto_candidates(crypto_selected), weights
        )[: self.settings.crypto_candidate_limit]
        stock_ranked = scoring.score_candidates(
            scoring.build_stock_candidates(stock_selected), weights
        )[: self.settings.stock_candidate_limit]
        shortlist = sorted(
            crypto_ranked + stock_ranked, key=lambda c: (-c.score, c.symbol)
        )
        items = llm.deterministic_items(
            shortlist, self.settings.final_crypto, self.settings.final_stocks
        )
        logger.info("Dry run: %d crypto + %d stock candidates, %d items",
                    len(crypto_ranked), len(stock_ranked), len(items))
        return ScanResult(
            run_id="dry-" + uuid.uuid4().hex[:8],
            created_at=ScanResult.now_iso(),
            market_open=is_us_market_open(),
            dry_run=True,
            llm_fallback=True,
            llm_error="dry run: LLM not called",
            items=items,
            sources_used=["sample_data"],
            candidates_considered={
                "crypto_selected": len(crypto_selected),
                "stocks_selected": len(stock_selected),
                "shortlist": len(shortlist),
            },
        )

    # -- helpers -----------------------------------------------------------

    def _client(self) -> HttpClient:
        if self._http is None:
            self._http = HttpClient(
                timeout=self.settings.scan_http_timeout,
                retries=self.settings.scan_http_retries,
            )
        return self._http


def _apply_bybit_ticker(candidate: Candidate, row: dict) -> None:
    price = _f(row.get("lastPrice"))
    high = _f(row.get("highPrice24h"))
    low = _f(row.get("lowPrice24h"))
    if price:
        candidate.price = price
    volatility = metrics.volatility_pct(high, low)
    if volatility is not None:
        candidate.volatility_pct = round(volatility, 4)
    candidate.distance_from_high_pct = _round(
        metrics.distance_from_reference(price, high)
    )
    candidate.distance_from_low_pct = _round(
        metrics.distance_from_reference(price, low)
    )
    turnover = _f(row.get("turnover24h"))
    if turnover:
        candidate.extra["turnover_24h"] = turnover


def is_us_market_open(now: datetime | None = None) -> bool:
    """Regular US session only (Mon-Fri 09:30-16:00 ET); holidays are not modelled."""
    moment = (now or datetime.now(timezone.utc)).astimezone(US_EASTERN)
    if moment.weekday() >= 5:
        return False
    return MARKET_OPEN <= moment.time() <= MARKET_CLOSE


def _safe_store(store: ScanStore, result: ScanResult) -> None:
    try:
        store.save(result)
    except Exception as exc:  # noqa: BLE001 — storage must never kill a scan
        logger.error("Could not store scan %s: %s", result.run_id, exc)


def _load_fixture(name: str) -> list[dict]:
    path = SAMPLE_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def _f(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _round(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


# --- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one TKKBOT market scan")
    parser.add_argument("--dry-run", action="store_true",
                        help="use bundled sample data; no network calls, no LLM")
    parser.add_argument("--no-telegram", action="store_true",
                        help="do not send the Telegram summary")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)

    settings = get_scan_settings()
    _setup_logging(settings.log_level)
    try:
        result = Scanner(settings).run(
            dry_run=args.dry_run, notify=not args.no_telegram
        )
    except ConfigError as exc:
        logger.error("%s", exc)
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except LockBusy as exc:
        logger.warning("%s", exc)
        return 3

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print(_summary(result))
    return 0


def _summary(result: ScanResult) -> str:
    lines = [
        f"Scan {result.run_id} at {result.created_at} "
        f"({'dry run' if result.dry_run else 'live'}, "
        f"US market {'open' if result.market_open else 'closed'})"
    ]
    if result.llm_fallback:
        lines.append(f"  LLM fallback: {result.llm_error}")
    for item in result.items:
        volatility = (
            f"{item.volatility_pct:.2f}%" if item.volatility_pct is not None else "n/a"
        )
        lines.append(
            f"  {item.rank:>2}. {item.symbol:<8} {item.asset_type:<6} "
            f"vol {volatility:>7}  {item.direction_bias:<7} {item.confidence:>3}%  "
            f"{item.thesis[:60]}"
        )
    if result.sources_failed:
        lines.append(
            "  failed sources: " + ", ".join(s.name for s in result.sources_failed)
        )
    return "\n".join(lines)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
        force=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
