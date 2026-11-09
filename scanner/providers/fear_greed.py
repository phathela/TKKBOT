"""Alternative.me Fear & Greed index — a one-line sentiment input to the LLM."""
from __future__ import annotations

import logging

from .base import HttpClient, Provider, ProviderError

logger = logging.getLogger(__name__)

URL = "https://api.alternative.me/fng/"


class FearGreedProvider(Provider):
    name = "fear_greed"

    def current(self) -> dict | None:
        payload = self.http.get_json(URL, params={"limit": 1})
        rows = (payload or {}).get("data") or []
        if not rows:
            raise ProviderError("fear & greed returned no data")
        row = rows[0]
        try:
            return {
                "value": int(row.get("value")),
                "classification": str(row.get("value_classification") or ""),
            }
        except (TypeError, ValueError) as exc:
            raise ProviderError("fear & greed payload unusable") from exc
