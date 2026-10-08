"""Digested Propshaft asset paths produced by tools/build_assets.py."""

import json
from functools import cache

from markupsafe import Markup

from .config import GENERATED
from .jsonvalue import as_dict


@cache
def manifest() -> dict[str, str]:
    loaded = as_dict(json.loads((GENERATED / "manifest.json").read_text())) or {}
    paths: dict[str, str] = {}
    for name, entry in loaded.items():
        fields = as_dict(entry)
        digested = fields.get("digested_path") if fields is not None else None
        if isinstance(digested, str):
            paths[name] = digested
    return paths


def has_asset(name: str) -> bool:
    return name in manifest()


def asset(name: str) -> str:
    return "/assets/" + manifest().get(name, name)


@cache
def stylesheets() -> Markup:
    return Markup((GENERATED / "stylesheets.html").read_text())


@cache
def importmap() -> Markup:
    return Markup((GENERATED / "importmap.html").read_text())
