"""Fetch public HTTP metadata using a pinned, validated IP on every redirect."""

import asyncio
import http.client
import ipaddress
import socket
import ssl
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from fastapi import APIRouter, Request
from starlette.responses import Response

from .http import json_response, login
from .richtext import plain_text

router = APIRouter(include_in_schema=False)
LIMIT = 1024 * 1024


class Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.metadata: dict[str, str] = {}
        self.in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta":
            name = attributes.get("property") or attributes.get("name")
            content = attributes.get("content")
            if name and content:
                self.metadata[name] = content
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data


def fetch(url: str) -> dict[str, str]:
    for _ in range(4):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("invalid URL")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        if not addresses or not all(ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("private address")
        raw = socket.create_connection((str(addresses[0][4][0]), port), timeout=5)
        if parsed.scheme == "https":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=5)
        connection.sock = raw
        try:
            connection.request(
                "GET",
                (parsed.path + ("?" + parsed.query if parsed.query else "")) or "/",
                headers={"Host": parsed.netloc, "User-Agent": "Campfire", "Accept": "text/html"},
            )
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.getheader("Location", ""))
                continue
            if response.status != 200 or "text/html" not in response.getheader("Content-Type", ""):
                raise ValueError("no metadata")
            body = response.read(LIMIT + 1)
            if len(body) > LIMIT:
                raise ValueError("metadata too large")
            document = Document()
            document.feed(body.decode("utf-8", errors="replace"))
            meta = document.metadata
            image = meta.get("og:image")
            return {
                "url": url,
                "title": meta.get("og:title") or document.title,
                "description": meta.get("og:description") or meta.get("description", ""),
                "image": urljoin(url, image) if image else "",
                "site_name": meta.get("og:site_name", parsed.hostname),
            }
        finally:
            connection.close()
    raise ValueError("too many redirects")


@router.post("/unfurl_link")
async def unfurl(request: Request) -> Response:
    ctx = login(request)
    url = ctx.params.get("url")
    try:
        data = await asyncio.get_running_loop().run_in_executor(None, fetch, url)
    except ValueError, OSError, http.client.HTTPException:
        return Response(status_code=204)
    data["title"] = plain_text(data["title"])
    data["description"] = plain_text(data["description"])
    if not data["title"] or not data["description"]:
        return Response(status_code=204)
    return json_response(data)
