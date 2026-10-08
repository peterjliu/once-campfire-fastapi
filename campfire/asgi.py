"""The ASGI application: FastAPI routers behind the Campfire and compression middleware."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from starlette.responses import Response

from . import cable, config, jobs, opengraph
from .compression import CompressionMiddleware
from .http import Halt
from .middleware import CampfireMiddleware
from .routes import account, boosts, messages, public, rooms, searches, sessions, storage, users


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    cable.bind_loop(asyncio.get_running_loop())
    if config.settings.start_jobs:
        jobs.start()
    relay = asyncio.create_task(cable.relay()) if config.settings.redis_url else None
    try:
        yield
    finally:
        if relay is not None:
            relay.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await relay
        if config.settings.start_jobs:
            await asyncio.get_running_loop().run_in_executor(None, jobs.stop)


async def halted(_: Request, exc: Exception) -> Response:
    assert isinstance(exc, Halt)
    return exc.response


def build() -> FastAPI:
    application = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    application.add_exception_handler(Halt, halted)
    for module in (sessions, rooms, messages, boosts, searches, users, account, storage):
        application.include_router(module.router)
    application.include_router(opengraph.router)
    application.add_api_websocket_route("/cable", cable.endpoint)
    # The static catch-all stays last.
    application.include_router(public.router)
    application.add_middleware(CampfireMiddleware)
    application.add_middleware(CompressionMiddleware)
    return application


app = build()
