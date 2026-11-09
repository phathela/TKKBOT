"""HTTP plumbing shared by every provider.

One :class:`HttpClient` is created per scan run, so its cache is exactly the
"caching within a scan run" the scan needs: two stages asking for the same
Bybit ticker pay for it once.

Every request has a timeout, retries with exponential backoff, honours
``429``/``Retry-After``, and raises :class:`ProviderError` so the caller can
record the source as failed and carry on with the others.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# Never sleep longer than this per attempt, even if a server asks for more —
# a scan runs on a schedule, and a 5-minute stall is worse than a failed source.
MAX_RETRY_AFTER_SECONDS = 15.0


class ProviderError(RuntimeError):
    """A provider could not supply usable data."""


class HttpClient:
    """Small JSON GET client with retries, backoff and a per-run cache."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        retries: int = 2,
        backoff_seconds: float = 0.5,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "TKKBOT-scanner/1.0"},
            follow_redirects=True,
        )
        self._retries = max(0, int(retries))
        self._backoff = max(0.0, float(backoff_seconds))
        self._sleep = sleep
        self._cache: dict[str, Any] = {}

    # -- public API --------------------------------------------------------

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        key = self._cache_key(url, params, headers)
        if key in self._cache:
            return self._cache[key]
        payload = self._request("GET", url, params=params, headers=headers)
        self._cache[key] = payload
        return payload

    def post_json(
        self,
        url: str,
        body: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """POST a JSON body. Never cached: a POST is a side effect."""
        return self._request("POST", url, headers=headers, body=body)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        last_error = "no attempt made"
        for attempt in range(self._retries + 1):
            try:
                response = self._client.request(
                    method, url, params=params, headers=headers, json=body
                )
            except httpx.HTTPError as exc:  # timeouts, DNS, connection resets
                last_error = f"{type(exc).__name__}: {exc}"
                self._pause(attempt)
                continue

            if response.status_code == 429:
                delay = _retry_after_seconds(response) or self._backoff * (2 ** attempt)
                last_error = f"rate limited (429), retry after {delay:g}s"
                logger.warning("Rate limited by %s; waiting %.1fs",
                               _safe_host(url), delay)
                self._sleep(min(delay, MAX_RETRY_AFTER_SECONDS))
                continue
            if 500 <= response.status_code < 600:
                last_error = f"HTTP {response.status_code}"
                self._pause(attempt)
                continue
            if response.status_code >= 400:
                # 4xx other than 429 will not improve on retry: fail fast.
                raise ProviderError(
                    f"{_safe_host(url)} returned HTTP {response.status_code}"
                )
            try:
                return response.json()
            except ValueError as exc:
                raise ProviderError(f"{_safe_host(url)} returned non-JSON") from exc

        raise ProviderError(f"{_safe_host(url)} failed: {last_error}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _pause(self, attempt: int) -> None:
        delay = self._backoff * (2 ** attempt)
        if delay > 0:
            self._sleep(min(delay, MAX_RETRY_AFTER_SECONDS))

    @staticmethod
    def _cache_key(
        url: str,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
    ) -> str:
        query = urlencode(sorted((str(k), str(v)) for k, v in (params or {}).items()))
        # Headers matter only insofar as they select a dataset (API keys are the
        # same within a run); the URL+params identify the response.
        return f"{url}?{query}"


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form; fall back to exponential backoff


def _safe_host(url: str) -> str:
    """Host only — never the full URL, which may carry an API key in a query."""
    try:
        return httpx.URL(url).host
    except Exception:  # noqa: BLE001 — logging must never raise
        return "provider"


class Provider:
    """Base class; subclasses are stateless apart from the shared client."""

    name = "provider"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.name}>"
