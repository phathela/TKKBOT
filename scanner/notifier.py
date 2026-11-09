"""Telegram delivery. Optional: without a token/chat id, nothing is sent."""
from __future__ import annotations

import logging

from .providers.base import HttpClient, ProviderError
from .schemas import RankedItem, ScanResult

logger = logging.getLogger(__name__)

TELEGRAM_MAX_CHARS = 4000  # the API caps at 4096; leave room for the footer
FOOTER = "\N{WARNING SIGN} Not financial advice — informational only."


def build_message(result: ScanResult, previous_symbols: set[str] | None = None) -> str:
    previous = previous_symbols or set()
    market = "open" if result.market_open else "closed (stock data may be stale)"
    lines = [
        f"\N{CHART WITH UPWARDS TREND} TKKBOT opportunities — {result.created_at.replace('T', ' ')}",
        f"US market: {market}",
    ]
    if result.llm_fallback:
        lines.append("LLM unavailable — deterministic ranking")
    lines.append("")
    for item in result.items:
        lines.extend(_item_lines(item, previous))
    if result.sources_failed:
        failed = ", ".join(source.name for source in result.sources_failed)
        lines.append(f"\N{INFORMATION SOURCE} Partial data (failed: {failed})")
    lines.append("")
    lines.append(FOOTER)
    message = "\n".join(lines)
    return message[:TELEGRAM_MAX_CHARS]


def _item_lines(item: RankedItem, previous: set[str]) -> list[str]:
    volatility = (
        f"{item.volatility_pct:.2f}%" if item.volatility_pct is not None else "n/a"
    )
    marker = " \N{SPARKLES} NEW" if item.symbol not in previous and previous else ""
    header = (
        f"{item.rank}. {item.symbol} ({item.asset_type}) — {volatility} · "
        f"{item.direction_bias} · {item.confidence}%{marker}"
    )
    return [header, f"   {_one_line(item.thesis)}"]


def _one_line(text: str, limit: int = 160) -> str:
    first = text.strip().split("\n")[0]
    if ". " in first:
        first = first.split(". ")[0] + "."
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"


def send_message(
    text: str,
    *,
    token: str,
    chat_id: str,
    http: HttpClient,
) -> bool:
    if not token or not chat_id:
        logger.info("Telegram not configured; scan not delivered")
        return False
    try:
        http.post_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
    except ProviderError as exc:
        # The token is part of the URL, so log the error text only.
        logger.error("Telegram delivery failed: %s", exc)
        return False
    logger.info("Telegram message sent (%d chars)", len(text))
    return True
