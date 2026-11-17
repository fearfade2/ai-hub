"""OpenAI-compatible endpoints: /v1/models, /v1/chat/completions (incl. streaming)."""
from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import config
from .pricing import Pricing
from .store import Store
from .upstream import Upstream, UpstreamError

router = APIRouter()


def error(status: int, message: str, error_type: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": error_type,
                                   "code": None}}, status_code=status)


def bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return None


def pricing(request: Request) -> Pricing:
    return request.app.state.pricing


def store(request: Request) -> Store:
    return request.app.state.store


async def authorize(request: Request) -> tuple | None:
    secret = bearer(request)
    if not secret:
        return None
    return store(request).verify_key(secret)


@router.get("/v1/models")
async def list_models(request: Request):
    key = await authorize(request)
    if key is None:
        return error(401, "Неверный API-ключ", "authentication_error")
    models = await request.app.state.upstream.models()
    data = [{"id": model["id"], "object": "model", "owned_by": "ai-hub"}
            for model in models]
    return JSONResponse({"object": "list", "data": data})


def _quota_block(key, totals: dict) -> JSONResponse | None:
    if totals["requests"] >= key["daily_requests"]:
        return error(429, "Дневной лимит запросов исчерпан", "rate_limit_error")
    if key["daily_usd"] > 0 and totals["usd"] >= key["daily_usd"]:
        return error(429, "Дневной лимит стоимости исчерпан", "rate_limit_error")
    return None


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    key = await authorize(request)
    if key is None:
        return error(401, "Неверный API-ключ", "authentication_error")
    try:
        body = await request.json()
    except ValueError:
        return error(400, "Тело запроса должно быть JSON")
    if not isinstance(body, dict):
        return error(400, "Тело запроса должно быть JSON-объектом")
    model = body.get("model")
    messages = body.get("messages")
    if not isinstance(model, str) or not model.strip():
        return error(400, "Не указана модель")
    if not isinstance(messages, list) or not messages:
        return error(400, "Не указаны сообщения")
    if len(messages) > config.MAX_CONTEXT_MESSAGES:
        return error(400, f"Слишком длинный контекст (максимум {config.MAX_CONTEXT_MESSAGES} сообщений)")
    if any(not isinstance(message, dict) or not isinstance(message.get("content", ""), str)
           for message in messages):
        return error(400, "Каждое сообщение требует role и content")

    totals = store(request).today_totals(key["id"])
    block = _quota_block(key, totals)
    if block is not None:
        return block

    stream = bool(body.get("stream"))
    safe_body = {"model": model, "messages": messages, "stream": stream}
    max_tokens = body.get("max_tokens")
    if isinstance(max_tokens, int) and 0 < max_tokens <= config.MAX_OUTPUT_TOKENS:
        safe_body["max_tokens"] = max_tokens
    else:
        safe_body["max_tokens"] = config.MAX_OUTPUT_TOKENS

    upstream: Upstream = request.app.state.upstream
    try:
        response = await upstream.completion(model, messages, stream=stream,
                                              max_tokens=safe_body["max_tokens"])
    except UpstreamError as upstream_error:
        status = upstream_error.status
        headers = {}
        if upstream_error.retry_after:
            headers["Retry-After"] = str(upstream_error.retry_after)
        return JSONResponse({"error": {"message": str(upstream_error),
                                       "type": "upstream_error"}},
                            status_code=status, headers=headers)

    if stream:
        async def relay_and_bill() -> AsyncIterator[bytes]:
            completion_tokens = 0
            try:
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    chunk = line + "\n"
                    if line.startswith("data: "):
                        payload = line[6:].strip()
                        if payload and payload != "[DONE]":
                            try:
                                decoded = json.loads(payload)
                                usage = decoded.get("usage") or {}
                                completion_tokens = max(
                                    completion_tokens, int(usage.get("completion_tokens") or 0))
                            except ValueError:
                                pass
                    yield chunk.encode()
                yield b"data: [DONE]\n\n"
            finally:
                usage = _extract_usage_headers(response)
                prompt_tokens = usage.get("prompt_tokens", _estimate_tokens(messages))
                if not completion_tokens:
                    completion_tokens = usage.get("completion_tokens", 0)
                cost = pricing(request).cost(model, prompt_tokens, completion_tokens)
                store(request).record_usage(key["id"], model, prompt_tokens=prompt_tokens,
                                            completion_tokens=completion_tokens, usd=cost)
                await response.aclose()

        return StreamingResponse(relay_and_bill(),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})
    data = response.json()
    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or _estimate_tokens(messages))
    completion_tokens = int(usage.get("completion_tokens") or 0)
    cost = pricing(request).cost(model, prompt_tokens, completion_tokens)
    store(request).record_usage(key["id"], model, prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens, usd=cost)
    return JSONResponse(data)


def _estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        total += len(str(message.get("content", ""))) // 4 + 8
    return max(1, total)


def _extract_usage_headers(response) -> dict:
    usage = {}
    try:
        raw = response.headers.get("x-upstream-usage")
        if raw:
            usage = json.loads(raw)
    except ValueError:
        pass
    return usage if isinstance(usage, dict) else {}
