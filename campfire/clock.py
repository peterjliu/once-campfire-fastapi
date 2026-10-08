"""UTC clock and the Rails SQLite datetime text format."""

import os
from datetime import UTC, datetime


def now() -> datetime:
    frozen = os.environ.get("CAMPFIRE_FROZEN_TIME")
    if frozen:
        return datetime.fromisoformat(frozen.replace("Z", "+00:00"))
    return datetime.now(UTC)


def format_time(value: datetime) -> str:
    """Rails stores `YYYY-MM-DD HH:MM:SS[.ffffff]` in UTC; zero microseconds are omitted."""
    value = value.astimezone(UTC) if value.tzinfo else value
    text = value.strftime("%Y-%m-%d %H:%M:%S")
    return text + f".{value.microsecond:06d}" if value.microsecond else text


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_optional(value: str | None) -> datetime | None:
    return parse_time(value) if value else None


def iso_millis(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
