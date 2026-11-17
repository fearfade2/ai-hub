"""Calls the upstream OpenAI-compatible provider under the service API key."""
from __future__ import annotations

import asyncio

import httpx

from . import config

_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class UpstreamError(Exception):
    def __init__(self, message: str, status: int = 502, retry_after: int = 0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class Upstream:
    def __init__(self, *, base_url: str | None = None, key: str | None = None):
        self.base_url = (base_url or config.UPSTREAM_BASE_URL).rstrip("/")
        self.key = key if key is not None else config.UPSTREAM_KEY

    @property
    def ready(self) -> bool:
        return bool(self.base_url) and bool(self.key)

    async def _request(self, path: str, body: dict) -> httpx.Response:
        if not self.ready:
            raise UpstreamError("Апстрим моделей не настроен", 503)
        try:
            async with httpx.AsyncClient(
                    base_url=self.base_url, timeout=config.UPSTREAM_TIMEOUT_SECONDS,
                    headers={"Authorization": f"Bearer {self.key}",
                             "Content-Type": "application/json"}) as client:
                return await client.post(path, json=body)
        except httpx.TimeoutException:
            raise UpstreamError("Апстрим не ответил вовремя", 504) from None
        except httpx.HTTPError:
            raise UpstreamError("Ошибка соединения с апстримом", 502) from None

    async def models(self) -> list[dict]:
        if not self.ready:
            return []
        try:
            async with httpx.AsyncClient(
                    base_url=self.base_url, timeout=20,
                    headers={"Authorization": f"Bearer {self.key}"}) as client:
                response = await client.get("/models")
                if response.status_code != 200:
                    return []
                data = response.json()
                return [item for item in data.get("data", [])
                        if isinstance(item, dict) and item.get("id")]
        except (httpx.HTTPError, ValueError):
            return []

    async def completion(self, model: str, messages: list[dict], *,
                         stream: bool = False, max_tokens: int | None = None,
                         override_key: str | None = None) -> httpx.Response:
        body = {"model": model, "messages": messages, "stream": stream}
        if max_tokens:
            body["max_tokens"] = max_tokens
        response = await self._request("/chat/completions", body)
        if response.status_code in _RETRYABLE_STATUS and not stream:
            retry_after = 0
            if response.status_code == 429:
                retry_after = min(60, int(response.headers.get("retry-after", "15") or 15))
            raise UpstreamError(
                f"Апстрим временно недоступен (HTTP {response.status_code})",
                503, retry_after)
        if response.status_code >= 500:
            raise UpstreamError(f"Апстрим вернул HTTP {response.status_code}", 502)
        if response.status_code == 401:
            raise UpstreamError("Ключ апстрима отклонён", 502)
        if response.status_code >= 400:
            raise UpstreamError(
                f"Апстрим отклонил запрос (HTTP {response.status_code})", 400)
        return response
