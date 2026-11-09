"""Typed shapes for every stage of a scan.

Provider rows are normalised into :class:`CryptoMarket` / :class:`StockQuote`;
the deterministic stage produces :class:`Candidate`; the LLM stage must return
:class:`RankedItem` objects, and :class:`ScanResult` is what gets stored,
served and notified.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

AssetType = Literal["crypto", "stock"]
DirectionBias = Literal["bullish", "bearish", "neutral"]


class CryptoMarket(BaseModel):
    """One row from a crypto market-cap listing."""

    symbol: str
    name: str = ""
    price: float = 0.0
    high_24h: float | None = None
    low_24h: float | None = None
    volume_24h: float = 0.0
    market_cap: float = 0.0
    change_24h: float | None = None
    change_7d: float | None = None
    change_30d: float | None = None
    source: str = ""

    @field_validator("symbol")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


class StockQuote(BaseModel):
    symbol: str
    name: str = ""
    price: float = 0.0
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    avg_volume: float | None = None
    change_pct: float | None = None
    source: str = ""
    stale: bool = False

    @field_validator("symbol")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


class Candidate(BaseModel):
    """A shortlisted asset with its computed, pre-LLM metrics."""

    asset_type: AssetType
    symbol: str
    name: str = ""
    price: float
    volatility_pct: float | None = None
    volume_spike: float | None = None
    momentum_pct: float | None = None
    liquidity_usd: float | None = None
    atr14: float | None = None
    change_24h_pct: float | None = None
    change_7d_pct: float | None = None
    distance_from_high_pct: float | None = None
    distance_from_low_pct: float | None = None
    score: float = 0.0
    components: dict[str, float] = Field(default_factory=dict)
    extra: dict[str, float | str | None] = Field(default_factory=dict)


class Headline(BaseModel):
    title: str
    source: str = ""
    url: str = ""
    published_at: str = ""


class AssetAnalysis(BaseModel):
    """The model's view of one asset: shared by ranked rows and the BTC anchor."""

    asset_type: AssetType
    symbol: str
    name: str = ""
    price: float = Field(ge=0)
    volatility_pct: float | None = None
    direction_bias: DirectionBias
    confidence: int = Field(ge=0, le=100)
    thesis: str = Field(min_length=1, max_length=600)
    key_risks: list[str] = Field(default_factory=list, max_length=6)
    suggested_entry_zone: str | None = None
    suggested_stop: float | None = None
    suggested_target: float | None = None
    time_horizon: str | None = None

    @field_validator("symbol")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("key_risks")
    @classmethod
    def _clean_risks(cls, value: list[str]) -> list[str]:
        cleaned = [str(item).strip()[:200] for item in value if str(item).strip()]
        return cleaned[:6]


class RankedItem(AssetAnalysis):
    """One row of the final Top 10, as returned by the LLM (or the fallback)."""

    rank: int = Field(ge=1, le=10)


class AnchorItem(AssetAnalysis):
    """The market anchor (BTC), analysed every scan whether or not it is ranked.

    ``rank`` is set only when BTC also made the Top 10; the row is pinned on the
    dashboard either way, so BTC is never missing from the page.
    """

    rank: int | None = None


class LLMResponse(BaseModel):
    """Envelope the model must produce."""

    items: list[RankedItem]
    anchor: AnchorItem | None = None

    @model_validator(mode="after")
    def _ten_ranks(self) -> "LLMResponse":
        ranks = sorted(item.rank for item in self.items)
        if ranks != list(range(1, len(self.items) + 1)):
            raise ValueError("ranks must be unique and contiguous from 1")
        return self


class SourceStatus(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class ScanResult(BaseModel):
    run_id: str
    created_at: str
    market_open: bool
    dry_run: bool = False
    llm_fallback: bool = False
    llm_error: str | None = None
    items: list[RankedItem] = Field(default_factory=list)
    # BTC's own view, always present so the dashboard can pin it regardless of
    # whether it made the Top 10. It is not an 11th ranked opportunity.
    anchor: AnchorItem | None = None
    sources_used: list[str] = Field(default_factory=list)
    sources_failed: list[SourceStatus] = Field(default_factory=list)
    discrepancies: list[str] = Field(default_factory=list)
    candidates_considered: dict[str, int] = Field(default_factory=dict)
    new_symbols: list[str] = Field(default_factory=list)

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
