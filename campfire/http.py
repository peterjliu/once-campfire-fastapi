"""Response helpers and the early-exit exception handlers use for authorization."""

from typing import NoReturn

import orjson
from starlette.requests import Request
from starlette.responses import Response

from .config import GENERATED
from .request_context import RequestContext, context_of


class Halt(Exception):
    """Stop handling and send `response` (login redirects, 403/404)."""

    def __init__(self, response: Response) -> None:
        super().__init__(response.status_code)
        self.response = response


def redirect(location: str) -> Response:
    return Response(status_code=302, headers={"location": location}, media_type="text/html")


def status(code: int, body: str = "") -> Response:
    return Response(body, status_code=code, media_type="text/html")


def json_response(
    value: object, code: int = 200, headers: dict[str, str] | None = None
) -> Response:
    return Response(
        orjson.dumps(value), status_code=code, media_type="application/json", headers=headers
    )


def _not_found_page() -> bytes:
    page = GENERATED / "public/404.html"
    return page.read_bytes() if page.is_file() else b"<h1>Not Found</h1>"


NOT_FOUND = _not_found_page()


def not_found_response() -> Response:
    return Response(NOT_FOUND, status_code=404, media_type="text/html")


def not_found() -> NoReturn:
    raise Halt(not_found_response())


def halt(response: Response) -> NoReturn:
    raise Halt(response)


def login(request: Request) -> RequestContext:
    """A signed-in browser user; bots may not use browser routes."""
    ctx = context_of(request)
    if ctx.current_user is None:
        ctx.session_data["return_to_after_authenticating"] = ctx.full_path
        halt(redirect("/session/new"))
    if ctx.authenticated_by_bot:
        halt(status(403))
    return ctx


def administrator(request: Request) -> RequestContext:
    ctx = login(request)
    if ctx.user.role != 1:
        halt(status(403))
    return ctx
