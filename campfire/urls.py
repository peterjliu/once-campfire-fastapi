"""Signed paths rendered into pages and fragments."""

from datetime import datetime

from . import rails


def version_time(value: object) -> str:
    return value.strftime("%Y%m%d%H%M%S") if isinstance(value, datetime) else ""


def avatar(user_id: int, *updated: datetime) -> str:
    return (
        "/users/"
        + rails.signed_id("User", int(user_id), "avatar")
        + "/avatar"
        + ("?v=" + version_time(updated[0]) if updated else "")
    )
