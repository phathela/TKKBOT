"""NewsAPI.org business headlines (optional, NEWS_API_KEY)."""
from __future__ import annotations

import logging

from ..schemas import Headline
from .base import HttpClient, Provider, ProviderError

logger = logging.getLogger(__name__)

URL = "https://newsapi.org/v2/top-headlines"


class NewsApiProvider(Provider):
    name = "newsapi"

    def __init__(self, http: HttpClient, api_key: str) -> None:
        super().__init__(http)
        self._api_key = api_key

    def headlines(self, *, limit: int = 8) -> list[Headline]:
        payload = self.http.get_json(
            URL,
            params={
                "category": "business",
                "language": "en",
                "pageSize": max(1, min(limit, 20)),
            },
            headers={"X-Api-Key": self._api_key},
        )
        rows = (payload or {}).get("articles") or []
        if not isinstance(rows, list):
            raise ProviderError("newsapi returned an unexpected payload")
        headlines: list[Headline] = []
        for row in rows:
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            headlines.append(
                Headline(
                    title=title,
                    source=str((row.get("source") or {}).get("name") or "newsapi"),
                    url=str(row.get("url") or ""),
                    published_at=str(row.get("publishedAt") or ""),
                )
            )
        return headlines
