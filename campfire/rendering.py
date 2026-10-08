"""Jinja environment and page rendering for the retained Turbo/Stimulus templates."""

import html
import json
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from jinja2 import ChainableUndefined, Environment, FileSystemLoader
from jinja2.runtime import Macro
from markupsafe import Markup
from starlette.responses import Response

from . import config, rails
from .assets import asset, importmap, stylesheets
from .clock import now
from .jsonvalue import as_dict, as_list
from .models import ADMINISTRATOR, Account
from .presentation import UserView, all_emoji, user_view
from .queries import account as load_account
from .queries import has_attachment
from .request_context import RequestContext
from .urls import avatar, version_time

type Fields = dict[str, object]

HTML_TYPE = "text/html; charset=utf-8"


def epoch(value: object) -> int:
    return int(value.timestamp() * 1000) if isinstance(value, datetime) else 0


def iso(value: object) -> str:
    if not isinstance(value, datetime):
        return ""
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _translations() -> dict[str, list[tuple[str, str]]]:
    loaded = as_dict(json.loads((config.TEMPLATES.parent / "translations.json").read_text()))
    result: dict[str, list[tuple[str, str]]] = {}
    for key, entries in (loaded or {}).items():
        pairs = [as_list(entry) or [] for entry in as_list(entries) or []]
        result[key] = [(str(pair[0]), str(pair[1])) for pair in pairs if len(pair) == 2]
    return result


TRANSLATIONS = _translations()


def translate(key: str) -> Markup:
    entries = "".join(
        "<dt>" + html.escape(flag) + '</dt><dd class="margin-none">' + html.escape(text) + "</dd>"
        for flag, text in TRANSLATIONS.get(key, [])
    )
    return Markup(
        '<details class="position-relative" data-controller="popup" '
        'data-action="keydown.esc-&gt;popup#close toggle-&gt;popup#toggle '
        'click@document-&gt;popup#closeOnClickOutside" '
        'data-popup-orientation-top-class="popup-orientation-top">'
        '<summary class="btn" tabindex="-1"><img width="20" height="20" aria-hidden="true" '
        'class="color-icon" src="'
        + asset("globe.svg")
        + '"><span class="for-screen-reader">Translate</span></summary>'
        '<div class="language-list-menu shadow" data-popup-target="menu">'
        '<dl class="language-list">' + entries + "</dl></div></details>"
    )


INVOLVEMENTS = {
    "everything": "Notifying about all messages",
    "mentions": "Notifying about @ mentions",
    "nothing": "Notifications are off",
    "invisible": "Notifications are off and room invisible in sidebar",
}


def involvement_order(room_type: str) -> list[str]:
    if room_type == "Rooms::Direct":
        return ["everything", "nothing"]
    return ["mentions", "everything", "nothing", "invisible"]


def next_involvement(room_type: str, value: str) -> str:
    order = involvement_order(room_type)
    return order[(order.index(value) + 1) % len(order)] if value in order else order[0]


@dataclass(frozen=True, slots=True)
class Reaction:
    Character: str
    Title: str


REACTIONS = [
    Reaction(character, title)
    for character, title in (
        ("👍", "Thumbs up"),
        ("👏", "Clapping"),
        ("👋", "Waving hand"),
        ("💪", "Muscle"),
        ("❤️", "Red heart"),
        ("😂", "Face with tears of joy"),
        ("🎉", "Party popper"),
        ("🔥", "Fire"),
    )
]


def _first_name(text: object) -> str:
    return text.split()[0] if isinstance(text, str) and text.split() else ""


def _lower(text: object) -> str:
    return text.lower() if isinstance(text, str) else ""


def _get(mapping: object, key: object) -> object:
    if isinstance(mapping, dict):
        return cast(dict[object, object], mapping).get(key, False)
    return False


def _printf(fmt: str, *args: object) -> str:
    return fmt % args


def _qrpath(text: object) -> str:
    return "/qr_code/" + rails.b64(str(text).encode()).replace("/", "_").replace("+", "-")


def _agent(text: object) -> Fields:
    return {"Name": text, "Platform": "", "Browser": text}


def _help_mailto(user: object) -> Markup:
    email = user.Email if isinstance(user, UserView) else ""
    return Markup('href="mailto:' + html.escape(email) + '"')


def _human_involvement(value: object) -> str:
    return INVOLVEMENTS.get(str(value), "")


def _bot_command(origin: object, room: object, key: object, attach: object) -> str:
    return f"curl -d 'Hello!' {origin}/rooms/{room}/{key}/messages"


env = Environment(
    loader=FileSystemLoader(config.TEMPLATES),
    autoescape=True,
    undefined=ChainableUndefined,
    auto_reload=False,
)
cast(dict[str, object], env.globals).update(
    asset=asset,
    avatar=avatar,
    versionTime=version_time,
    epoch=epoch,
    iso=iso,
    stylesheets=stylesheets,
    importmap=importmap,
    translate=translate,
    firstName=_first_name,
    lower=_lower,
    len=len,
    get=_get,
    allEmoji=all_emoji,
    printf=_printf,
    qrpath=_qrpath,
    humanInvolvement=_human_involvement,
    nextInvolvement=next_involvement,
    reactions=lambda: REACTIONS,
    agent=_agent,
    helpMailto=_help_mailto,
    botCommand=_bot_command,
)
MACROS: dict[str, Macro] = {
    name: macro
    for name, macro in vars(env.get_template("pages.html").module).items()
    if isinstance(macro, Macro)
}


def render(name: str, data: object) -> str:
    """Render one template macro. Every page and fragment passes through here."""
    return str(MACROS[name.replace("-", "_")](data))


def _account_fields(account: Account | None) -> Fields:
    if account is None:
        return {"UpdatedAt": now(), "HasLogo": False}
    settings = as_dict(json.loads(account.settings or "{}")) if account.settings else None
    restrict = bool(settings.get("restrict_room_creation_to_administrators")) if settings else False
    return {
        "ID": account.id,
        "Name": account.name,
        "JoinCode": account.join_code,
        "UpdatedAt": account.updated_at,
        "HasLogo": has_attachment("Account", account.id, "logo"),
        "RestrictRoomCreation": restrict,
        "RestrictRooms": restrict,
    }


def body_class(screen: str) -> str:
    if screen == "search":
        return "sidebar searches"
    return "sidebar" if screen in ("room", "welcome") else screen


def context(ctx: RequestContext, screen: str, fields: Fields) -> Fields:
    account = load_account()
    account_fields = _account_fields(account)
    user = ctx.current_user
    data: Fields = {
        "User": user_view(user),
        "Account": account_fields,
        "Screen": screen,
        "BodyClass": body_class(screen),
        "Title": "Campfire",
        "Frame": bool(ctx.header("turbo-frame")),
        "Origin": ctx.origin,
        "Version": "once-campfire-fastapi",
        "VAPIDPublicKey": config.settings.vapid_public_key,
        "CustomStyles": Markup("<style>" + account.custom_styles + "</style>")
        if account is not None and account.custom_styles
        else "",
        "Messages": [],
        "RecentSearches": [],
        "RoomsStream": rails.sign_stream("rooms"),
        "UserRoomsStream": "",
        "Notice": "",
        "Error": "",
        "Reload": False,
        "Chat": screen == "room",
        "ReturnRoom": ctx.session_data.get("last_room_id", ""),
        "Query": "",
    }
    if user is not None:
        data["UserRoomsStream"] = rails.sign_stream(rails.user_rooms_stream(user.id))
        data["CanCreateRooms"] = user.role == ADMINISTRATOR or not account_fields.get(
            "RestrictRoomCreation"
        )
    data.update(fields)
    return data


def page(ctx: RequestContext, name: str, status: int = 200, **fields: object) -> Response:
    body = render(name, context(ctx, name, fields))
    return Response(body, status_code=status, media_type=HTML_TYPE)


def fragment_response(body: str) -> Response:
    return Response(body, media_type=HTML_TYPE)


def turbo_stream(
    action: str, target: str, fragment: str | None = None, *, maintain_scroll: bool = False
) -> str:
    """A `<turbo-stream>` element; attribute values are escaped like Rails' tag builder."""
    attributes = (
        f'action="{html.escape(action, quote=True)}" target="{html.escape(target, quote=True)}"'
    )
    if maintain_scroll:
        attributes += ' maintain_scroll="true"'
    template = "" if fragment is None else f"<template>{fragment}</template>"
    return f"<turbo-stream {attributes}>{template}</turbo-stream>"


TURBO_STREAM = "text/vnd.turbo-stream.html"


def turbo_response(body: str, status: int = 200) -> Response:
    return Response(body, status_code=status, media_type=TURBO_STREAM)
