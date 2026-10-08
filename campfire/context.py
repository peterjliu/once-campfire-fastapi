"""Request-scoped values needed below the HTTP layer."""

from contextvars import ContextVar

# OpenGraph previews hide links back to the current host.
request_host: ContextVar[str] = ContextVar("campfire_request_host", default="")
