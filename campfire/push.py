"""Web Push delivery pinned to a public DNS result while retaining TLS hostname checks."""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false, reportMissingTypeStubs=false

import http.client
import io
import ipaddress
import json
import socket
import ssl
from urllib.parse import SplitResult, urlsplit

import requests
from pywebpush import WebPushException, webpush
from requests.adapters import BaseAdapter
from requests.models import PreparedRequest
from requests.structures import CaseInsensitiveDict

from . import config

PUSH_HOSTS = (
    "jmt17.google.com",
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "web.push.apple.com",
    "notify.windows.com",
)


def resolve(endpoint: str) -> tuple[SplitResult, str]:
    uri = urlsplit(endpoint)
    if (
        uri.scheme != "https"
        or uri.port not in (None, 443)
        or uri.username
        or uri.password
        or not uri.hostname
    ):
        raise ValueError("invalid endpoint")
    hostname = uri.hostname
    if not any(hostname == h or hostname.endswith("." + h) for h in PUSH_HOSTS):
        raise ValueError("unpermitted endpoint")
    addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or not all(ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("private endpoint")
    return uri, str(addresses[0][4][0])


def permitted_endpoint(endpoint: str | None) -> bool:
    if not endpoint:
        return False
    try:
        resolve(endpoint)
    except ValueError, TypeError, OSError:
        return False
    return True


class PinnedAdapter(BaseAdapter):
    """Transport that connects to the address `resolve` validated, never re-resolving the host."""

    def send(
        self,
        request: PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float | None, float | None] | None = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        endpoint = request.url or ""
        uri, address = resolve(endpoint)
        hostname = uri.hostname or ""
        seconds = timeout if isinstance(timeout, float | int) else 7
        # Direct TCP socket: proxy environment variables cannot cause a second DNS lookup.
        raw = socket.create_connection((address, 443), timeout=seconds)
        secure = ssl.create_default_context().wrap_socket(raw, server_hostname=hostname)
        connection = http.client.HTTPConnection(hostname, 443, timeout=seconds)
        connection.sock = secure
        try:
            sent = {key: value for key, value in request.headers.items()}
            sent["Host"] = hostname
            body = request.body
            if isinstance(body, str):
                body = body.encode()
            if body is not None and not isinstance(body, bytes):
                raise TypeError("streamed push bodies are not supported")
            connection.request(
                request.method or "POST",
                (uri.path + ("?" + uri.query if uri.query else "")) or "/",
                body=body,
                headers=sent,
            )
            reply = connection.getresponse()
            response = requests.Response()
            response.status_code = reply.status
            response.reason = reply.reason
            response.headers = CaseInsensitiveDict(dict(reply.getheaders()))
            response.raw = io.BytesIO(reply.read(1024 * 1024))
            response.url = endpoint
            response.request = request
            return response
        finally:
            connection.close()

    def close(self) -> None:
        return None


def pinned_session() -> requests.Session:
    """A session whose every request goes through `PinnedAdapter`, ignoring proxy settings."""
    session = requests.Session()
    session.trust_env = False
    session.adapters.clear()
    session.mount("https://", PinnedAdapter())
    return session


def send(
    endpoint: str, p256dh: str, auth: str, title: str, body: str, path: str, badge: int
) -> None:
    """Deliver one notification; raises Gone for subscriptions the provider expired."""
    private = config.settings.vapid_private_key
    if not private or not permitted_endpoint(endpoint):
        return
    payload = json.dumps(
        {"title": title, "options": {"body": body, "data": {"path": path, "badge": badge}}}
    )
    try:
        webpush(
            {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
            payload,
            requests_session=pinned_session(),
            vapid_private_key=private,
            vapid_claims={"sub": config.settings.vapid_subject},
            timeout=7,
        )
    except WebPushException as error:
        response = error.response
        if response is not None and response.status_code in (404, 410):
            raise Gone from error
        # Other provider rejections are not retried (matching the Django port).


class Gone(Exception):
    """The push provider reports the subscription no longer exists."""
