"""Bounded immutable page responses after fresh authorization and SQLite epoch checks.

The epoch is `PRAGMA data_version` on a dedicated read-only observer connection, which
never writes and therefore sees local and foreign commits alike. The middleware captures
it before loading the current user; lookups compare it again, and admission rechecks it,
so a commit during authentication or rendering cannot publish an old snapshot under a new
epoch. Observer failures bypass the cache.
"""

import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import orjson
from starlette.responses import Response

from . import config
from .compression import RawHeaders, encode
from .request_context import CacheVersion, RequestContext

MAX_ENTRY = 1024 * 1024
ENTRY_OVERHEAD = 256
MAX_KEY = 2048


@dataclass(frozen=True, slots=True)
class Entry:
    body: bytes
    headers: RawHeaders
    size: int


class ResponseCache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.entries: OrderedDict[bytes, Entry] = OrderedDict()
        self.bytes = 0
        self.observer: sqlite3.Connection | None = None
        self.database: str | None = None
        self.version: int | None = None

    def clear(self) -> None:
        with self.lock:
            self._clear()
            if self.observer is not None:
                self.observer.close()
            self.observer = None
            self.database = None
            self.version = None

    def _clear(self) -> None:
        self.entries.clear()
        self.bytes = 0

    def _version(self) -> tuple[str, int]:
        database = str(config.settings.database_path)
        if database != self.database or self.observer is None:
            self._clear()
            if self.observer is not None:
                self.observer.close()
            self.observer = None
            self.observer = sqlite3.connect(
                Path(database).resolve().as_uri() + "?mode=ro",
                uri=True,
                check_same_thread=False,
                timeout=0,
            )
            self.database = database
            self.version = None
        row: tuple[int] = self.observer.execute("PRAGMA data_version").fetchone()
        if row[0] != self.version:
            self._clear()
            self.version = row[0]
        return database, row[0]

    def version_for_request(self) -> CacheVersion:
        with self.lock:
            try:
                return self._version()
            except sqlite3.Error:
                self._clear()
                return None

    def get(self, key: bytes) -> tuple[CacheVersion, Entry | None]:
        with self.lock:
            try:
                version = self._version()
            except sqlite3.Error:
                self._clear()
                return None, None
            return version, self.entries.get(key)

    def put(self, key: bytes, version: CacheVersion, entry: Entry, budget: int) -> None:
        if entry.size > min(budget, MAX_ENTRY):
            return
        with self.lock:
            try:
                if self._version() != version:
                    return
            except sqlite3.Error:
                self._clear()
                return
            if key in self.entries:
                return
            while self.entries and self.bytes + entry.size > budget:
                _, evicted = self.entries.popitem(last=False)
                self.bytes -= evicted.size
            self.entries[key] = entry
            self.bytes += entry.size


cache = ResponseCache()

CACHED_HEADERS = ("accept", "user-agent", "turbo-frame", "accept-encoding")


def cache_key(ctx: RequestContext, scope: str) -> bytes:
    assert ctx.current_user is not None and ctx.current_session is not None
    return orjson.dumps(
        [
            scope,
            ctx.current_user.id,
            ctx.current_session.token,
            ctx.session_data,
            ctx.scheme,
            ctx.host,
            ctx.full_path,
            ctx.cookies,
            *(ctx.header(name) for name in CACHED_HEADERS),
        ],
        option=orjson.OPT_SORT_KEYS,
    )


def cacheable(ctx: RequestContext) -> bool:
    return (
        config.settings.response_cache_bytes > 0
        and ctx.method == "GET"
        and ctx.current_session is not None
        and ctx.current_user is not None
        and not ctx.authenticated_by_bot
        and not ctx.session_data.get("flash")
    )


async def serve(
    ctx: RequestContext, scope: str, render: Callable[[], Awaitable[Response]]
) -> Response:
    """Serve one of the audited read pages; the caller has already authorized access."""
    budget = config.settings.response_cache_bytes
    if not cacheable(ctx):
        return await render()
    try:
        key = cache_key(ctx, scope)
    except TypeError:
        return await render()
    if len(key) > MAX_KEY:
        return await render()
    version, stored = cache.get(key)
    # Authentication captured rows before this point. A commit during that interval must
    # not admit them under a newer epoch.
    if version is None or version != ctx.cache_version:
        return await render()
    if stored is not None:
        return Response(stored.body, headers=_header_dict(stored.headers))
    original_session = dict(ctx.session_data)
    response = await render()
    headers, body = encode(
        response.status_code,
        list(response.raw_headers),
        bytes(response.body),
        ctx.header("accept-encoding"),
    )
    content_type = next((v for n, v in headers if n == b"content-type"), b"")
    if (
        response.status_code == 200
        and content_type.startswith(b"text/html")
        and not any(name == b"set-cookie" for name, _ in headers)
        and ctx.session_data == original_session
    ):
        size = len(key) + len(body) + sum(len(n) + len(v) for n, v in headers) + ENTRY_OVERHEAD
        cache.put(key, version, Entry(body, headers, size), budget)
    return Response(body, status_code=response.status_code, headers=_header_dict(headers))


def _header_dict(headers: RawHeaders) -> dict[str, str]:
    return {name.decode("latin-1"): value.decode("latin-1") for name, value in headers}
