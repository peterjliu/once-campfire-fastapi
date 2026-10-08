"""Deterministic gzip with explicit Accept-Encoding quality handling."""

import gzip

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MINIMUM = 200
COMPRESSIBLE_PREFIXES = ("text/", "application/json", "application/javascript", "image/svg+xml")
COMPRESSIBLE_EXACT = {"application/manifest+json", "application/xml"}


def accepts_gzip(header: str) -> bool:
    qualities: dict[str, float] = {}
    for item in header.split(","):
        coding, *parameters = item.strip().lower().split(";")
        quality = 1.0
        for parameter in parameters:
            name, separator, raw = parameter.strip().partition("=")
            if name == "q":
                try:
                    quality = float(raw) if separator else 0.0
                except ValueError:
                    quality = 0.0
        coding = coding.strip()
        quality = quality if 0.0 <= quality <= 1.0 else 0.0
        qualities[coding] = min(qualities.get(coding, quality), quality)
    gzip_quality = qualities.get("gzip", qualities.get("*", 0.0))
    return gzip_quality > 0.0 and gzip_quality >= qualities.get("identity", 0.0)


def compressible(content_type: str) -> bool:
    mime = content_type.split(";", 1)[0].strip().lower()
    return mime.startswith(COMPRESSIBLE_PREFIXES) or mime in COMPRESSIBLE_EXACT


def compress(body: bytes) -> bytes:
    # Token-free HTML has no per-render secret requiring random padding; mtime=0 keeps
    # cached gzip bytes deterministic.
    return gzip.compress(body, compresslevel=6, mtime=0)


type RawHeaders = list[tuple[bytes, bytes]]


def _vary(headers: RawHeaders) -> RawHeaders:
    for index, (name, value) in enumerate(headers):
        if name == b"vary":
            if b"accept-encoding" in value.lower():
                return headers
            headers[index] = (name, value + b", Accept-Encoding")
            return headers
    headers.append((b"vary", b"Accept-Encoding"))
    return headers


def encode(
    status: int, headers: RawHeaders, body: bytes, accept_encoding: str
) -> tuple[RawHeaders, bytes]:
    """Select the representation (identity or gzip) for one complete response body."""
    names = {name for name, _ in headers}
    if status != 200 or b"content-encoding" in names or len(body) < MINIMUM:
        return headers, body
    content_type = next((v for n, v in headers if n == b"content-type"), b"").decode("latin-1")
    if not compressible(content_type):
        return headers, body
    headers = _vary(headers)
    if not accepts_gzip(accept_encoding):
        return headers, body
    compressed = compress(body)
    if len(compressed) >= len(body):
        return headers, body
    rewritten = [(n, v) for n, v in headers if n != b"content-length"]
    rewritten.append((b"content-encoding", b"gzip"))
    rewritten.append((b"content-length", str(len(compressed)).encode()))
    return rewritten, compressed


class CompressionMiddleware:
    """Buffers single-message bodies and compresses them; streamed bodies pass through."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        accept = ""
        for name, value in scope["headers"]:
            if name == b"accept-encoding":
                accept = value.decode("latin-1")
                break
        start: Message | None = None

        async def wrapped(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] != "http.response.body" or start is None:
                await send(message)
                return
            if message.get("more_body", False):
                # Streamed bodies are forwarded unchanged. Cached representations are
                # already selected; `encode` is idempotent on its own output.
                await send(start)
                start = None
                await send(message)
                return
            headers, body = encode(
                int(start["status"]), list(start["headers"]), message.get("body", b""), accept
            )
            await send(
                {"type": "http.response.start", "status": start["status"], "headers": headers}
            )
            start = None
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, wrapped)
