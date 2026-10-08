"""Typed view of the redis-py calls Campfire uses (the library leaves them partially unknown)."""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, cast

import redis
import redis.asyncio as aioredis


class Pipeline(Protocol):
    def publish(self, channel: str, message: bytes) -> object: ...

    def execute(self) -> list[object]: ...

    def __enter__(self) -> Pipeline: ...

    def __exit__(self, *exc: object) -> None: ...


class Client(Protocol):
    def pipeline(self, transaction: bool = ...) -> Pipeline: ...

    def eval(self, script: str, numkeys: int, *keys_and_args: str | int) -> object: ...


class PubSub(Protocol):
    async def subscribe(self, *channels: str) -> None: ...

    def listen(self) -> AsyncIterator[dict[str, object]]: ...

    async def aclose(self) -> None: ...


class AsyncClient(Protocol):
    def pubsub(self) -> PubSub: ...

    async def aclose(self) -> None: ...


def connect(url: str) -> Client:
    return cast(Client, redis.from_url(url))


def connect_async(url: str) -> AsyncClient:
    return cast(AsyncClient, aioredis.from_url(url))


def publish_all(client: Client, channel: str, payloads: Sequence[bytes]) -> None:
    """One round trip for an ordered batch."""
    with client.pipeline(transaction=False) as pipeline:
        for payload in payloads:
            pipeline.publish(channel, payload)
        pipeline.execute()


async def messages(url: str, channel: str) -> AsyncIterator[bytes]:
    """Every payload published to `channel`, until cancelled."""
    client = connect_async(url)
    subscription = client.pubsub()
    try:
        await subscription.subscribe(channel)
        async for event in subscription.listen():
            data = event.get("data")
            if event.get("type") == "message" and isinstance(data, bytes):
                yield data
    finally:
        await subscription.aclose()
        await client.aclose()
