"""FastAPI application: gateway routes, health endpoint, bot loop."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .gateway import router as gateway_router
from .pricing import Pricing
from .store import Store
from .upstream import Upstream


def public_url() -> str:
    """The HTTPS base under which /v1 is reachable from the internet."""
    return (os.getenv("HUB_PUBLIC_URL") or "").rstrip("/")


config.upstream_public = public_url


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = Store()
    app.state.pricing = Pricing(config.PRICING_FILE)
    app.state.upstream = Upstream()
    bot_task = None
    if config.BOT_TOKEN:
        from .bot import Bot
        bot = Bot(app.state.store, app.state.upstream)
        bot_task = asyncio.create_task(bot.run())
    try:
        yield
    finally:
        if bot_task:
            bot_task.cancel()
            try:
                await bot_task
            except (asyncio.CancelledError, Exception):
                pass
        app.state.store.close()


app = FastAPI(title="AI Hub", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=lifespan)
app.include_router(gateway_router)


@app.get("/health")
async def health(request: Request):
    """200 while the process is alive; the box keepalive polls this."""
    return JSONResponse({"ok": True,
                         "upstream_ready": bool(request.app.state.upstream.ready),
                         "bot_configured": bool(config.BOT_TOKEN)})
