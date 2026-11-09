"""CryptoPanic headlines (optional)."""
from __future__ import annotations

import logging

from ..schemas import Headline
from .base import HttpClient, Provider, ProviderError

logger = logging.getLogger(__name__)

URL = "https://cryptopanic.com/api/v1/posts/"


class CryptoPanicProvider(Provider):
    name = "cryptopanic"

    def __init__(self, http: HttpClient, api_key: str) -> None:
        super().__init__(http)
        self._api_key = api_key

    def headlines(self, *, limit: int = 8) -> list[Headline]:
        payload = self.http.get_json(
            URL,
            params={"auth_token": self._api_key, "public": "true", "kind": "news"},
        )
        rows = (payload or {}).get("results") or []
        if not isinstance(rows, list):
            raise ProviderError("cryptopanic returned an unexpected payload")
        headlines: list[Headline] = []
        for row in rows[:limit]:
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            headlines.append(
                Headline(
                    title=title,
                    source=str((row.get("source") or {}).get("title") or "cryptopanic"),
                    url=str(row.get("url") or ""),
                    published_at=str(row.get("published_at") or ""),
                )
            )
        return headlines
