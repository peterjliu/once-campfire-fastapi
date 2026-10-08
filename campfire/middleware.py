"""Sessions, authentication, bans, method/suffix rewriting and Fetch Metadata protection.

One pure ASGI middleware replaces Django's session and security middleware. Order matters
for the page cache: the SQLite epoch is captured before the current user is loaded.
"""

import secrets
from datetime import timedelta
from http.cookies import CookieError, SimpleCookie
from urllib.parse import quote

from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import config, db, rails
from .clock import format_time, now
from .context import request_host
from .jsonvalue import as_dict
from .queries import bot_user, session_user
from .request_context import CONTEXT_KEY, Params, RequestContext
from .response_cache import cache
from .storage import Upload

TWENTY_YEARS = 20 * 365 * 86400
MAX_BODY = 100 * 1024 * 1024
FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")
OVERRIDES = ("PATCH", "PUT", "DELETE")
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"SAMEORIGIN"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
]


def request_allowed(
    method: str, scheme: str, host: str, origin: str | None, site: str | None, force_ssl: bool
) -> bool:
    """Rust's Fetch Metadata policy, evaluated against the trusted effective URL."""
    if method in ("GET", "HEAD"):
        return True
    if origin is not None and origin != f"{scheme}://{host}":
        return False
    if site is None:
        return scheme == "http" and not force_ssl
    return site in ("same-origin", "same-site")


def banned(address: str) -> bool:
    return db.exists("SELECT 1 FROM bans WHERE ip_address=?", (address,))


def parse_cookies(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for chunk in header.split(";"):
        name, separator, raw = chunk.strip().partition("=")
        if separator and name and name not in cookies:
            cookies[name] = raw.strip().strip('"')
    return cookies


def cookie_header(
    name: str, raw: str, *, http_only: bool, secure: bool, max_age: int = TWENTY_YEARS
) -> bytes:
    cookie = SimpleCookie()
    try:
        cookie[name] = quote(raw, safe="")
    except CookieError:
        return b""
    morsel = cookie[name]
    morsel["path"] = "/"
    morsel["max-age"] = max_age
    morsel["samesite"] = "Lax"
    if max_age == 0:
        morsel["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
    if http_only:
        morsel["httponly"] = True
    if secure:
        morsel["secure"] = True
    return morsel.OutputString().encode("latin-1")


def bot_key_for(ctx: RequestContext) -> str:
    from_query = ctx.query("bot_key")
    if from_query:
        return from_query
    pieces = ctx.path.strip("/").split("/")
    if len(pieces) > 3 and pieces[0] == "rooms" and pieces[3] == "messages":
        return pieces[2]
    return ""


def bot_route(ctx: RequestContext) -> bool:
    pieces = ctx.path.strip("/").split("/")
    return (
        ctx.authenticated_by_bot
        and len(pieces) >= 4
        and pieces[0] == "rooms"
        and pieces[3] == "messages"
    )


def signed_disk_upload(ctx: RequestContext) -> bool:
    if ctx.method != "PUT" or not ctx.path.startswith("/rails/active_storage/disk/"):
        return False
    try:
        payload = as_dict(rails.verify(ctx.path.rsplit("/", 1)[1], "ActiveStorage", "blob_token"))
    except ValueError, TypeError:
        return False
    return payload is not None and "key" in payload


def strip_format_suffix(path: str) -> str:
    if path.startswith(("/assets/", "/rails/active_storage/", "/webmanifest")):
        return path
    for suffix in (".json", ".turbo_stream"):
        if path.endswith(suffix):
            return path[: -len(suffix)]
    return path


async def read_body(receive: Receive) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk: bytes = message.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay(body: bytes) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


async def parse_params(scope: Scope, body: bytes, content_type: str) -> Params:
    if content_type.startswith(FORM_TYPES):
        request = Request(scope, _replay(body))
        form = await request.form(max_files=1000, max_fields=10000, max_part_size=MAX_BODY)
        params = Params()
        for name, value in form.multi_items():
            if isinstance(value, UploadFile):
                data = await value.read()
                if value.filename or data:
                    params.files[name] = Upload(
                        value.filename or "file", value.content_type or "", data
                    )
            else:
                params.fields.append((name, value))
        await form.close()
        return params
    if content_type.startswith("application/json"):
        try:
            import orjson

            loaded = as_dict(orjson.loads(body))
        except ValueError:
            loaded = None
        return Params(json=loaded or {})
    return Params()


def _headers(scope: Scope) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_name, raw_value in scope["headers"]:
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        headers[name] = (
            headers[name] + "; " + value if name == "cookie" and name in headers else value
        )
    return headers


async def load_session(ctx: RequestContext) -> None:
    encrypted = ctx.cookies.get("_campfire_session")
    data: dict[str, object] = {}
    if encrypted:
        try:
            data = as_dict(rails.decrypt_cookie("_campfire_session", encrypted)) or {}
        except ValueError:
            data = {}
    ctx.session_original = dict(data)
    if "session_id" not in data:
        data["session_id"] = secrets.token_hex(16)
    ctx.session_data = data


async def authenticate(ctx: RequestContext) -> None:
    raw = ctx.cookies.get("session_token")
    if raw:
        try:
            token = rails.verify_cookie("session_token", raw)
        except ValueError, TypeError:
            token = None
        found = session_user(token) if isinstance(token, str) else None
        if found is not None:
            session, user = found
            ctx.current_session, ctx.current_user = session, user
            if session.last_active_at < now() - timedelta(hours=1):
                await db.awrite(
                    touch_session, session.id, ctx.header("user-agent"), ctx.remote_addr
                )
    key = bot_key_for(ctx)
    if key and ctx.current_user is None:
        bot = bot_user(key)
        if bot is not None:
            ctx.current_user = bot
            ctx.authenticated_by_bot = True


def touch_session(session_id: int, user_agent: str, address: str) -> None:
    with db.transaction():
        stamp = format_time(now())
        db.run(
            "UPDATE sessions SET last_active_at=?,updated_at=?,user_agent=?,ip_address=? "
            "WHERE id=?",
            (stamp, stamp, user_agent, address, session_id),
        )


def response_cookies(ctx: RequestContext) -> list[tuple[bytes, bytes]]:
    cookies: list[tuple[bytes, bytes]] = []
    secure = ctx.secure
    if ctx.session_data != ctx.session_original:
        value = rails.encrypt_cookie(
            "_campfire_session", ctx.session_data, now() + timedelta(days=20 * 365)
        )
        cookies.append(
            (
                b"set-cookie",
                cookie_header("_campfire_session", value, http_only=True, secure=secure),
            )
        )
    if ctx.new_session_token is not None:
        value = rails.sign_cookie(
            "session_token", ctx.new_session_token, now() + timedelta(days=20 * 365)
        )
        cookies.append(
            (b"set-cookie", cookie_header("session_token", value, http_only=True, secure=secure))
        )
    elif ctx.clear_session_token:
        cookies.append(
            (
                b"set-cookie",
                cookie_header("session_token", "", http_only=False, secure=False, max_age=0),
            )
        )
    if ctx.last_room is not None:
        cookies.append(
            (
                b"set-cookie",
                cookie_header("last_room", str(ctx.last_room), http_only=False, secure=secure),
            )
        )
    return cookies


def plain(status: int, send: Send) -> Message:
    return {"type": "http.response.start", "status": status, "headers": SECURITY_HEADERS}


class CampfireMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method: str = scope["method"]
        head = method == "HEAD"
        headers = _headers(scope)
        client = scope.get("client")
        host = headers.get("host") or "localhost"
        query: bytes = scope.get("query_string", b"")
        ctx = RequestContext(
            method="GET" if head else method,
            scheme=scope.get("scheme", "http"),
            host=host,
            path=scope["path"],
            query_string=query.decode("latin-1"),
            remote_addr=client[0] if client else "",
            headers=headers,
            cookies=parse_cookies(headers.get("cookie", "")),
        )
        if head:
            ctx.method = "HEAD"
        if method == "GET" and config.settings.response_cache_bytes > 0:
            ctx.cache_version = cache.version_for_request()
        await load_session(ctx)
        await authenticate(ctx)
        token = request_host.set(host.split(":")[0])
        try:
            await self.dispatch(ctx, scope, receive, send, head)
        finally:
            request_host.reset(token)

    async def dispatch(
        self, ctx: RequestContext, scope: Scope, receive: Receive, send: Send, head: bool
    ) -> None:
        if banned(ctx.remote_addr):
            await self.reply(ctx, 403, send)
            return
        if ctx.method not in ("GET", "HEAD"):
            body = await read_body(receive)
            if body is None:
                await self.reply(ctx, 413, send)
                return
            ctx.body = body
            content_type = ctx.header("content-type").lower()
            ctx.params = await parse_params(scope, body, content_type)
            override = ctx.params.get("_method").upper() if ctx.method == "POST" else ""
            if override in OVERRIDES:
                ctx.method = override
            if not (bot_route(ctx) or signed_disk_upload(ctx)) and not request_allowed(
                ctx.method,
                ctx.scheme,
                ctx.host,
                ctx.headers.get("origin"),
                ctx.headers.get("sec-fetch-site"),
                config.settings.force_ssl,
            ):
                await self.reply(ctx, 422, send)
                return
            receive = _replay(body)
        routed = dict(scope)
        routed["method"] = "GET" if head else ctx.method
        routed["path"] = strip_format_suffix(ctx.path)
        routed["raw_path"] = routed["path"].encode()
        routed[CONTEXT_KEY] = ctx

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                extra = response_cookies(ctx) + SECURITY_HEADERS
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            elif message["type"] == "http.response.body" and head:
                message = {**message, "body": b""}
            await send(message)

        await self.app(routed, receive, wrapped)

    async def reply(self, ctx: RequestContext, status: int, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", b"0"),
                    *response_cookies(ctx),
                    *SECURITY_HEADERS,
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})
