"""Narrow decoded JSON (typed `object`) to concrete containers in one place."""

from typing import cast


def as_dict(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return None


def as_list(value: object) -> list[object] | None:
    if isinstance(value, list):
        return cast(list[object], value)
    return None


def as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default
