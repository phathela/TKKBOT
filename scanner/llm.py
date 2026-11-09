"""DeepSeek analysis of the deterministic shortlist.

One completion per scan. The response is parsed into :class:`LLMResponse` and
then checked against the numbers Python already computed: unknown tickers and
figures that contradict the data are rejected, with a single corrective retry.
If the second attempt fails too, the deterministic ranking is used and
``llm_fallback`` is set — a scan never dies because the model misbehaved.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from pydantic import ValidationError

from .providers.base import HttpClient, ProviderError
from .schemas import AnchorItem, AssetAnalysis, Candidate, Headline, LLMResponse, RankedItem

logger = logging.getLogger(__name__)

# The market anchor: analysed on every run, ranked or not, because the whole
# market moves with it and the dashboard always shows it.
ANCHOR_SYMBOL = "BTC"

# Relative/absolute tolerances for "does this number contradict the data?".
VOLATILITY_RELATIVE_TOLERANCE = 0.25
VOLATILITY_ABSOLUTE_TOLERANCE = 2.0  # percentage points
PRICE_RELATIVE_TOLERANCE = 0.20

SYSTEM_PROMPT = """You are a market analyst for a small automated scanner.

RULES
1. Use ONLY the figures in the user's JSON. Never invent prices, volumes,
   market caps or news. If something is missing or null, say so in the thesis.
2. Rank opportunities for a trader who can go long or short with leverage.
3. Be explicit about uncertainty. Confidence is your own 0-100 number.
4. Every symbol you return MUST be one of the candidates supplied. Do not add
   tickers from memory.
5. This is analysis for information only, not financial advice.
6. Reply with a single JSON object and nothing else (no markdown, no prose).

OUTPUT SHAPE
{"items": [
  {"rank": 1,
   "asset_type": "crypto" | "stock",
   "symbol": "BTC",
   "name": "Bitcoin",
   "price": 0.0,
   "volatility_pct": 0.0,
   "direction_bias": "bullish" | "bearish" | "neutral",
   "confidence": 0,
   "thesis": "max two sentences",
   "key_risks": ["..."],
   "suggested_entry_zone": "0.0-0.0",
   "suggested_stop": 0.0,
   "suggested_target": 0.0,
   "time_horizon": "e.g. 1-3 days"}
]}
Return exactly the requested number of crypto and stock items, ranked 1..N with
no gaps and no duplicates.

ANCHOR
Also return "anchor": the same shape for BTC, minus "rank" (add "rank" only if
BTC is one of your ranked items). BTC is the market benchmark: give your own
confidence for its next move even when it is not one of your picks. Omit the
key only if BTC is not among the candidates."""


@dataclass
class LLMAnalysis:
    items: list[RankedItem]
    anchor: AnchorItem | None = None
    fallback: bool = False
    error: str | None = None


class SelectionError(ValueError):
    """The model's answer cannot be trusted; retry or fall back."""


class DeepSeekClient:
    def __init__(
        self,
        http: HttpClient,
        *,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
    ) -> None:
        self.http = http
        self._api_key = api_key
        self._model = model
        self._url = base_url.rstrip("/") + "/chat/completions"

    def chat(self, messages: list[dict]) -> str:
        payload = self.http.post_json(
            self._url,
            {
                "model": self._model,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": 3000,
                # JSON mode: the API guarantees parseable JSON, not our schema.
                "response_format": {"type": "json_object"},
            },
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            return str(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("deepseek returned an unexpected payload") from exc


# --- public entry point ---------------------------------------------------


def analyse(
    *,
    shortlist: list[Candidate],
    headlines: list[Headline],
    client: DeepSeekClient,
    n_crypto: int,
    n_stocks: int,
    market_open: bool = True,
    fear_greed: dict | None = None,
) -> LLMAnalysis:
    prompt = build_user_prompt(
        shortlist=shortlist,
        headlines=headlines,
        n_crypto=n_crypto,
        n_stocks=n_stocks,
        market_open=market_open,
        fear_greed=fear_greed,
    )
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    content = ""
    error = ""

    for attempt in (1, 2):
        if attempt == 2:
            messages = messages + [
                {"role": "assistant", "content": content or "{}"},
                {
                    "role": "user",
                    "content": (
                        f"Your previous response was rejected: {error}\n"
                        "Return ONLY the corrected JSON object, using only the "
                        "supplied candidates and figures."
                    ),
                },
            ]
        try:
            content = client.chat(messages)
        except ProviderError as exc:
            error = f"DeepSeek request failed: {exc}"
            logger.warning("LLM attempt %d failed: %s", attempt, error)
            continue
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            error = f"response was not valid JSON ({exc.msg})"
            logger.warning("LLM attempt %d rejected: %s", attempt, error)
            continue
        try:
            items = validate_selection(data, shortlist, n_crypto, n_stocks)
        except SelectionError as exc:
            error = str(exc)
            logger.warning("LLM attempt %d rejected: %s", attempt, error)
            continue
        # The anchor is a convenience, not part of the ranking: a missing or
        # bad one must not cost a second API call. Fall back to the numbers.
        anchor = validate_anchor(data, shortlist) or anchor_item(shortlist)
        logger.info("LLM selected %d items", len(items))
        return LLMAnalysis(items=items, anchor=anchor)

    logger.warning("Falling back to deterministic ranking: %s", error)
    return LLMAnalysis(
        items=deterministic_items(shortlist, n_crypto, n_stocks),
        anchor=anchor_item(shortlist),
        fallback=True,
        error=error or "LLM unavailable",
    )


def build_user_prompt(
    *,
    shortlist: list[Candidate],
    headlines: list[Headline],
    n_crypto: int,
    n_stocks: int,
    market_open: bool,
    fear_greed: dict | None,
) -> str:
    candidates = [_candidate_payload(c) for c in shortlist]
    payload = {
        "as_of": _now_iso(),
        "us_market_open": market_open,
        "fear_greed": fear_greed,
        "requested": {"crypto": n_crypto, "stocks": n_stocks},
        "candidates": candidates,
        "headlines": [f"[{h.source}] {h.title}" for h in headlines][:12],
    }
    return (
        f"Select the {n_crypto} best crypto and {n_stocks} best stock "
        "opportunities from these candidates and rank all of them 1.."
        f"{n_crypto + n_stocks}. Also return the anchor object for BTC.\n\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def validate_selection(
    data: object,
    shortlist: list[Candidate],
    n_crypto: int,
    n_stocks: int,
) -> list[RankedItem]:
    """Parse and cross-check the model's answer. Raises :class:`SelectionError`."""
    if not isinstance(data, dict):
        raise SelectionError("response must be a JSON object with an 'items' list")
    raw_items = data.get("items")
    if raw_items is None and isinstance(data.get("opportunities"), list):
        raw_items = data["opportunities"]  # tolerate one common alias
    if not isinstance(raw_items, list):
        raise SelectionError("response is missing an 'items' list")

    try:
        parsed = LLMResponse(items=raw_items)
    except ValidationError as exc:
        raise SelectionError(_compact_validation_error(exc)) from exc

    expected = n_crypto + n_stocks
    if len(parsed.items) != expected:
        raise SelectionError(
            f"expected {expected} items ({n_crypto} crypto, {n_stocks} stocks), "
            f"got {len(parsed.items)}"
        )
    crypto_count = sum(1 for item in parsed.items if item.asset_type == "crypto")
    if crypto_count != n_crypto or len(parsed.items) - crypto_count != n_stocks:
        raise SelectionError(
            f"expected {n_crypto} crypto and {n_stocks} stocks, got "
            f"{crypto_count} crypto and {len(parsed.items) - crypto_count} stocks"
        )

    known = {(c.asset_type, c.symbol): c for c in shortlist}
    unknown = [
        f"{item.asset_type}:{item.symbol}"
        for item in parsed.items
        if (item.asset_type, item.symbol) not in known
    ]
    if unknown:
        raise SelectionError(
            "these symbols are not in the candidate list: " + ", ".join(unknown)
        )

    for item in parsed.items:
        candidate = known[(item.asset_type, item.symbol)]
        _check_numbers(item, candidate)

    return sorted(parsed.items, key=lambda item: item.rank)


def validate_anchor(
    data: object, shortlist: list[Candidate]
) -> AnchorItem | None:
    """The model's BTC view, or ``None`` when it is missing or contradicts the data.

    Never raises: unlike the ranked items, the anchor is not part of the scan's
    contract, so a bad one is logged and replaced by :func:`anchor_item`.
    """
    if not isinstance(data, dict):
        return None
    candidate = _anchor_candidate(shortlist)
    if candidate is None:
        return None
    raw = data.get("anchor", data.get("market_anchor", data.get("btc")))
    if not isinstance(raw, dict):
        logger.info("LLM returned no anchor for %s", ANCHOR_SYMBOL)
        return None
    try:
        anchor = AnchorItem(**raw)
    except ValidationError as exc:
        logger.warning("LLM anchor rejected: %s", _compact_validation_error(exc))
        return None
    if anchor.symbol != candidate.symbol:
        logger.warning(
            "LLM anchor is %s, expected %s; using the data instead",
            anchor.symbol, candidate.symbol,
        )
        return None
    try:
        _check_numbers(anchor, candidate)
    except SelectionError as exc:
        logger.warning("LLM anchor rejected: %s", exc)
        return None
    return anchor


def _anchor_candidate(shortlist: list[Candidate]) -> Candidate | None:
    for candidate in shortlist:
        if candidate.asset_type == "crypto" and candidate.symbol == ANCHOR_SYMBOL:
            return candidate
    return None


def anchor_item(shortlist: list[Candidate]) -> AnchorItem | None:
    """The deterministic BTC view, used when the model gives none."""
    candidate = _anchor_candidate(shortlist)
    if candidate is None:
        return None
    return AnchorItem(**_fallback_fields(candidate))


def _check_numbers(item: AssetAnalysis, candidate: Candidate) -> None:
    if candidate.volatility_pct is not None and item.volatility_pct is not None:
        allowed = max(
            VOLATILITY_ABSOLUTE_TOLERANCE,
            candidate.volatility_pct * VOLATILITY_RELATIVE_TOLERANCE,
        )
        if abs(item.volatility_pct - candidate.volatility_pct) > allowed:
            raise SelectionError(
                f"{item.symbol} volatility_pct {item.volatility_pct} contradicts "
                f"the computed {candidate.volatility_pct}"
            )
    if candidate.price > 0:
        gap = abs(item.price - candidate.price) / candidate.price
        if gap > PRICE_RELATIVE_TOLERANCE:
            raise SelectionError(
                f"{item.symbol} price {item.price} contradicts the quoted "
                f"{candidate.price}"
            )


# --- deterministic fallback ------------------------------------------------


def deterministic_items(
    shortlist: list[Candidate], n_crypto: int, n_stocks: int
) -> list[RankedItem]:
    """Rank by the pre-score alone when the LLM cannot be trusted."""
    crypto = [c for c in shortlist if c.asset_type == "crypto"][:n_crypto]
    stocks = [c for c in shortlist if c.asset_type == "stock"][:n_stocks]
    merged = sorted(crypto + stocks, key=lambda c: (-c.score, c.symbol))
    return [
        _fallback_item(position, candidate)
        for position, candidate in enumerate(merged, start=1)
    ]


def _fallback_item(rank: int, candidate: Candidate) -> RankedItem:
    return RankedItem(rank=rank, **_fallback_fields(candidate))


def _fallback_fields(candidate: Candidate) -> dict:
    """The deterministic narrative shared by a ranked row and the anchor."""
    volatility = candidate.volatility_pct
    momentum = candidate.momentum_pct or 0.0
    bias = "bullish" if momentum > 1 else "bearish" if momentum < -1 else "neutral"
    spike = candidate.volume_spike
    parts = [
        f"{candidate.symbol} shows a daily range of {volatility:.1f}%"
        if volatility is not None else f"{candidate.symbol} has no usable 24h range",
    ]
    if spike is not None:
        parts.append(f"on {spike:.1f}x its recent average volume")
    if candidate.momentum_pct is not None:
        parts.append(f"with {abs(momentum):.1f}% momentum")
    thesis = ", ".join(parts) + "."
    risk_pct = min(max((volatility or 3.0) / 2.0, 1.0), 10.0) / 100.0
    stop, target = _levels(candidate.price, risk_pct, bias)
    return {
        "asset_type": candidate.asset_type,
        "symbol": candidate.symbol,
        "name": candidate.name or candidate.symbol,
        "price": candidate.price,
        "volatility_pct": volatility,
        "direction_bias": bias,
        "confidence": int(round(40 + 55 * min(1.0, max(0.0, candidate.score)))),
        "thesis": thesis[:600],
        "key_risks": [
            "Ranked by the deterministic fallback: no narrative analysis.",
            "Volatility cuts both ways; a stop is essential.",
        ],
        "suggested_entry_zone": (
            f"{_fmt(candidate.price * 0.99)}-{_fmt(candidate.price * 1.01)}"
        ),
        "suggested_stop": stop,
        "suggested_target": target,
        "time_horizon": "1-3 days",
    }


def _levels(price: float, risk_pct: float, bias: str) -> tuple[float, float]:
    if bias == "bearish":
        return (
            round(price * (1 + risk_pct), 8),
            round(price * (1 - 2 * risk_pct), 8),
        )
    return (
        round(price * (1 - risk_pct), 8),
        round(price * (1 + 2 * risk_pct), 8),
    )


def _candidate_payload(candidate: Candidate) -> dict:
    return {
        "asset_type": candidate.asset_type,
        "symbol": candidate.symbol,
        "name": candidate.name,
        "price": candidate.price,
        "volatility_pct": candidate.volatility_pct,
        "volume_spike": candidate.volume_spike,
        "momentum_pct": candidate.momentum_pct,
        "liquidity_usd": candidate.liquidity_usd,
        "atr14": candidate.atr14,
        "change_24h_pct": candidate.change_24h_pct,
        "change_7d_pct": candidate.change_7d_pct,
        "distance_from_high_pct": candidate.distance_from_high_pct,
        "distance_from_low_pct": candidate.distance_from_low_pct,
        "prescore": candidate.score,
    }


def _compact_validation_error(exc: ValidationError) -> str:
    problems = []
    for error in exc.errors()[:4]:
        location = ".".join(str(part) for part in error.get("loc", ()))
        problems.append(f"{location}: {error.get('msg')}")
    return "invalid item(s): " + "; ".join(problems)


def _fmt(value: float) -> str:
    return f"{value:.6g}"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
