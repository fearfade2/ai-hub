"""End-to-end gateway tests against the FastAPI app with a moable upstream."""
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import httpx

from app import config
from app.api import app
from app.store import Store


def fake_completion_response(payload: dict, status: int = 200) -> httpx.Response:
    request = httpx.Request("POST", "https://upstream/v1/chat/completions")
    return httpx.Response(status, json=payload, request=request)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.sqlite",
                           daily_requests=5, daily_usd=1.0)
        app.state.store = self.store
        from app.pricing import Pricing
        app.state.pricing = Pricing(None)
        app.state.upstream = NS(
            ready=True,
            models=AsyncMock(return_value=[{"id": "glm-5.3"}]),
            completion=AsyncMock(side_effect=lambda *a, **k: fake_completion_response({
                "choices": [{"message": {"content": "привет"}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30}})))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.store.close()
        self.tmp.cleanup()

    def auth_headers(self, secret: str) -> dict:
        return {"Authorization": f"Bearer {secret}"}

    async def test_rejects_missing_and_bad_key(self):
        response = await self.client.get("/v1/models")
        self.assertEqual(response.status_code, 401)
        response = await self.client.get("/v1/models", headers=self.auth_headers("hub_bad"))
        self.assertEqual(response.status_code, 401)

    async def test_models_list_authorizes_with_valid_key(self):
        self.store.ensure_user(10, "alice")
        secret, _ = self.store.issue_key(10)
        response = await self.client.get("/v1/models", headers=self.auth_headers(secret))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["id"], "glm-5.3")

    async def test_chat_completion_bills_usage(self):
        self.store.ensure_user(20, "bob")
        secret, _ = self.store.issue_key(20)
        response = await self.client.post(
            "/v1/chat/completions", headers=self.auth_headers(secret),
            json={"model": "glm-5.3",
                  "messages": [{"role": "user", "content": "ку"}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "привет")
        key = self.store.verify_key(secret)
        totals = self.store.today_totals(key["id"])
        self.assertEqual(totals["requests"], 1)
        self.assertEqual(totals["pt"], 120)
        self.assertEqual(totals["ct"], 30)
        self.assertAlmostEqual(totals["usd"], 120 / 1e6 * 3.0 + 30 / 1e6 * 9.0, places=9)

    async def test_request_limit_blocks_before_dispatch(self):
        self.store.ensure_user(30, "carol", )
        self.store.set_quota(30, requests=1, usd=10.0)
        secret, _ = self.store.issue_key(30)
        first = await self.client.post(
            "/v1/chat/completions", headers=self.auth_headers(secret),
            json={"model": "m", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(first.status_code, 200)
        # The first dispatch happened; reset only the counter, keeping side effect.
        app.state.upstream.completion.reset_mock()
        second = await self.client.post(
            "/v1/chat/completions", headers=self.auth_headers(secret),
            json={"model": "m", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(second.status_code, 429)
        self.assertEqual(app.state.upstream.completion.await_count, 0)

    async def test_usd_limit_blocks_before_dispatch(self):
        self.store.ensure_user(40, "dave")
        self.store.set_quota(40, requests=100, usd=0.000001)
        secret, _ = self.store.issue_key(40)
        first = await self.client.post(
            "/v1/chat/completions", headers=self.auth_headers(secret),
            json={"model": "m", "messages": [{"role": "user", "content": "x"}]})
        # after first request the usd limit (tiny) is exceeded for the second
        second = await self.client.post(
            "/v1/chat/completions", headers=self.auth_headers(secret),
            json={"model": "m", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(second.status_code, 429)

    async def test_validation_rejects_bad_payloads(self):
        self.store.ensure_user(50, "erin")
        secret, _ = self.store.issue_key(50)
        headers = self.auth_headers(secret)
        cases = [
            ("no model", {"messages": [{"role": "user", "content": "x"}]}),
            ("no messages", {"model": "m"}),
            ("bad kind", {"model": 5, "messages": "text"}),
            ("too long", {"model": "m",
                          "messages": [{"role": "user", "content": "x"}] * (config.MAX_CONTEXT_MESSAGES + 1)}),
        ]
        for name, body in cases:
            response = await self.client.post("/v1/chat/completions",
                                              headers=headers, json=body)
            self.assertEqual(response.status_code, 400, name)

    async def test_upstream_error_maps_to_503_with_retry_after(self):
        from app.upstream import UpstreamError
        self.store.ensure_user(60, "frank")
        secret, _ = self.store.issue_key(60)
        app.state.upstream.completion = AsyncMock(
            side_effect=UpstreamError("Апстрим перегружен", 503, 30))
        response = await self.client.post(
            "/v1/chat/completions", headers=self.auth_headers(secret),
            json={"model": "m", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers.get("Retry-After"), "30")

    async def test_health_is_public(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])


if __name__ == "__main__":
    unittest.main()
