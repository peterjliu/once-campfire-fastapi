"""Health, PWA files, QR codes, logos and digested static assets."""

import io
import mimetypes
import threading

import orjson
from fastapi import APIRouter, Request
from starlette.responses import Response

from .. import config, db, rails
from ..assets import asset
from ..compression import RawHeaders, accepts_gzip, encode
from ..files import serve_file
from ..http import login, not_found, redirect, status
from ..media import find_variant, variant
from ..queries import account, attached_blob
from ..request_context import RequestContext, context_of
from ..storage import path_for, remove_attachment

router = APIRouter(include_in_schema=False)
PUBLIC = (config.GENERATED / "public").resolve()
HEALTH = b'<!doctype html><html><body style="background-color: green"></body></html>'
_static: dict[tuple[str, bool], tuple[int, RawHeaders, bytes]] = {}
_static_lock = threading.Lock()


@router.get("/up")
async def health() -> Response:
    return Response(HEALTH, media_type="text/html; charset=utf-8")


def static_response(ctx: RequestContext, relative: str) -> Response:
    """Immutable public files, with identity/gzip representations memoized per worker."""
    gzip = accepts_gzip(ctx.header("accept-encoding"))
    with _static_lock:
        cached = _static.get((relative, gzip))
    if cached is None:
        target = (PUBLIC / relative).resolve()
        if not target.is_relative_to(PUBLIC) or not target.is_file():
            not_found()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        cache_control = (
            "public, max-age=31536000, immutable"
            if relative.startswith("assets/")
            else "public, max-age=3600"
        )
        raw = target.read_bytes()
        headers: RawHeaders = [
            (b"content-type", content_type.encode()),
            (b"content-length", str(len(raw)).encode()),
            (b"accept-ranges", b"bytes"),
            (b"content-disposition", f'inline; filename="{target.name}"'.encode()),
            (b"cache-control", cache_control.encode()),
        ]
        headers, body = encode(200, headers, raw, "gzip" if gzip else "identity")
        cached = (200, headers, body)
        with _static_lock:
            _static[(relative, gzip)] = cached
    _, headers, body = cached
    response = Response(body)
    response.raw_headers = list(headers)
    return response


@router.get("/webmanifest")
@router.get("/webmanifest.json")
async def manifest() -> Response:
    found = account()
    icons: list[dict[str, str]] = [
        {"src": "/account/logo?size=small", "type": "image/png", "sizes": "192x192"},
        {"src": "/account/logo", "type": "image/png", "sizes": "512x512"},
        {"src": "/account/logo", "type": "image/png", "sizes": "512x512", "purpose": "maskable"},
    ]
    body = {
        "name": found.name if found else "Campfire",
        "icons": icons,
        "start_url": "/",
        "display": "standalone",
        "scope": "/",
        "description": "A chat app from the makers of Basecamp and HEY.",
        "categories": ["social", "business", "productivity"],
        "theme_color": "#ffffff",
        "background_color": "#ffffff",
        "shortcuts": [
            {"name": "New chat room", "url": "/rooms/opens/new"},
            {"name": "My profile", "url": "/users/me/profile"},
        ],
    }
    return Response(orjson.dumps(body), media_type="application/manifest+json")


SERVICE_WORKER = (config.REFERENCE / "app/views/pwa/service_worker.js").read_bytes()


@router.get("/service-worker")
@router.get("/service-worker.js")
async def service_worker() -> Response:
    return Response(SERVICE_WORKER, media_type="application/javascript")


@router.get("/qr_code/{encoded}")
async def qr_code(encoded: str) -> Response:
    import qrcode
    from qrcode.image.pil import PilImage

    try:
        value = rails.decode64(encoded).decode()
    except ValueError, UnicodeDecodeError:
        not_found()
    stream = io.BytesIO()
    qrcode.make(value, image_factory=PilImage).save(stream, format="PNG")
    return Response(stream.getvalue(), media_type="image/png")


@router.get("/account/logo")
async def logo(request: Request) -> Response:
    ctx = context_of(request)
    found = account()
    small = ctx.query("size") == "small"
    blob = attached_blob("Account", found.id, "logo") if found is not None else None
    if blob is not None:
        size = (192, 192) if small else (512, 512)
        processed = find_variant(blob, size, "png") or await db.awrite(variant, blob, size, "png")
        return serve_file(ctx, path_for(processed.key), "image/png", processed.filename)
    name = "logos/app-icon-192.png" if small else "logos/app-icon.png"
    return static_response(ctx, asset(name).lstrip("/"))


@router.delete("/account/logo")
async def remove_logo(request: Request) -> Response:
    ctx = login(request)
    if ctx.user.role != 1:
        return status(403)
    found = account()
    if found is not None:
        await db.awrite(remove_attachment, "Account", found.id, "logo")
    return redirect("/account/edit")


@router.get("/{path:path}")
async def static(request: Request, path: str) -> Response:
    return static_response(context_of(request), path)
