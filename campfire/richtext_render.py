"""Presentation of sanitized Action Text: mentions, embeds, OpenGraph, sounds, autolinks."""

import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from . import rails
from .assets import asset, has_asset
from .context import request_host
from .jsonvalue import as_dict, as_str
from .queries import user_by_id
from .richtext import (
    ATTACHMENT,
    attached_blob,
    attachment_attributes,
    mention_ids,
    plain_text,
    sanitize,
)
from .urls import avatar

SOUNDS: dict[str, dict[str, object]] = {
    name: as_dict(value) or {}
    for name, value in (
        as_dict(json.loads((Path(__file__).with_name("sounds.json")).read_text())) or {}
    ).items()
}
PLAY = re.compile(r"/play \w+")
LINK = re.compile(r"(?:https?://|www\.)[^\s<>]+")


def safe_preview_url(value: str | None, host: str = "") -> str | None:
    if not value:
        return None
    try:
        uri = urlsplit(value)
        name = uri.hostname or ""
    except ValueError:
        return None
    tld = name.split(".")[-1]
    if (
        uri.scheme not in ("http", "https")
        or "%" in name
        or "." not in name
        or not re.search("[a-z]", tld, re.I)
        or tld.lower().startswith("0x")
        or name.rstrip(".").lower() == host.rstrip(".").lower()
    ):
        return None
    return value


class MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.description = ""
        self.href: str | None = None
        self.image: str | None = None
        self.mode: str | None = None
        self.stack: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.stack.append(self.mode)
        classes = attributes.get("class") or ""
        if "og-embed__title" in classes:
            self.mode = "title"
        if "og-embed__description" in classes:
            self.mode = "description"
        if tag == "a" and self.mode == "title":
            self.href = attributes.get("href")
        if tag == "img":
            self.image = attributes.get("src")

    def handle_endtag(self, tag: str) -> None:
        if self.stack:
            self.mode = self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self.mode == "title":
            self.title += data
        if self.mode == "description":
            self.description += data


def opengraph_html(attributes: dict[str, str], host: str = "") -> str:
    content = html.unescape(attributes.get("content", ""))
    title = attributes.get("filename", "")
    description = attributes.get("caption", "")
    href: str | None = attributes.get("href")
    image: str | None = attributes.get("url")
    if not title and content:
        parser = MetadataParser()
        parser.feed(content)
        title, description, href, image = (
            parser.title,
            parser.description,
            parser.href,
            parser.image,
        )
    if not title:
        return ""
    href = safe_preview_url(href, host)
    image = safe_preview_url(image, host)
    title = html.escape(title[:279] + "…" if len(title) > 280 else title)
    description = html.escape(description[:559] + "…" if len(description) > 560 else description)
    if href:
        title = (
            f'<a href="{html.escape(href, quote=True)}" rel="noreferrer" target="_blank">'
            + title
            + "</a>"
        )
    picture = (
        '<div class="og-embed__image"><img src="'
        + html.escape(image, quote=True)
        + '" class="image center" alt=""></div>'
        if image
        else ""
    )
    return (
        '<figure class="attachment attachment--content attachment--og">'
        '<actiontext-opengraph-embed><div class="og-embed gap"><div class="og-embed__content">'
        f'<div class="og-embed__title">{title}</div>'
        f'<div class="og-embed__description">{description}</div></div>'
        + picture
        + "</div></actiontext-opengraph-embed></figure>"
    )


class AutoLink(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.excluded = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(self.get_starttag_text() or "")
        if tag in ("a", "code", "pre"):
            self.excluded += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag: str) -> None:
        self.parts.append("</" + tag + ">")
        if tag in ("a", "code", "pre"):
            self.excluded = max(0, self.excluded - 1)

    def handle_entityref(self, name: str) -> None:
        self.parts.append("&" + name + ";")

    def handle_charref(self, name: str) -> None:
        self.parts.append("&#" + name + ";")

    def handle_data(self, data: str) -> None:
        if self.excluded:
            self.parts.append(data)
            return
        self.parts.append(LINK.sub(_link, data))


def _link(match: re.Match[str]) -> str:
    text = match[0]
    tail = ""
    while text and text[-1] in ".,!?:;)":
        tail = text[-1] + tail
        text = text[:-1]
    href = text if text.startswith(("http://", "https://")) else "http://" + text
    return (
        f'<a href="{html.escape(href, quote=True)}" target="_blank">'
        + html.escape(text)
        + "</a>"
        + tail
    )


def _mention_html(user_id: int) -> str | None:
    user = user_by_id(user_id)
    if user is None:
        return None
    return (
        f'<span class="mention" sgid="{html.escape(rails.sgid("User", user.id))}">'
        f'<a href="/users/{user.id}" class="btn avatar" title="{html.escape(user.title)}">'
        f'<img src="{avatar(user.id, user.updated_at)}" width="48" height="48" aria-hidden="true">'
        f"</a> {html.escape(user.name)}</span>"
    )


def _attachment_html(raw: str, host: str) -> str:
    from .media import VARIABLE_TYPES, representation_url
    from .storage import blob_url

    attributes = attachment_attributes(raw)
    ids = mention_ids(raw)
    if ids:
        mention = _mention_html(ids[0])
        if mention is not None:
            return mention
    if "application/vnd.actiontext.opengraph-embed" in attributes.get("content-type", ""):
        return opengraph_html(attributes, host)
    blob = attached_blob(attributes)
    if blob is None:
        return ""
    url = blob_url(blob)
    name = html.escape(blob.filename)
    if blob.content_type in VARIABLE_TYPES:
        return (
            f'<figure class="attachment attachment--preview"><a href="{url}" '
            f'data-action="lightbox#open" data-lightbox-url-value="{url}">'
            f'<img src="{representation_url(blob)}" alt="{name}"></a>'
            f"<figcaption>{name}</figcaption></figure>"
        )
    return f'<figure class="attachment"><a href="{url}">{name}</a></figure>'


def _sound(text: str) -> str | None:
    name = text.split()[1]
    sound = SOUNDS.get(name)
    if sound is None or not has_asset(name + ".mp3"):
        return None
    label = html.escape(as_str(sound.get("text")))
    image = as_str(sound.get("image"))
    if image:
        label = (
            f'<img src="{asset(image)}" width="{sound.get("width")}" '
            f'height="{sound.get("height")}" class="align--middle">'
        )
    return (
        '<div class="sound" data-controller="sound" data-action="messages:play-&gt;sound#play" '
        f'data-sound-url-value="{asset(name + ".mp3")}">'
        '<button class="btn btn--plain" data-action="sound#play">🔊</button>' + label + "</div>"
    )


def render_body(body: str, host: str = "") -> str:
    host = host or request_host.get()
    body = sanitize(body)
    if "<action-text-attachment" in body:
        body = ATTACHMENT.sub(lambda match: _attachment_html(match[0], host), body)
    if "/play" in body:
        text = plain_text(body)
        if PLAY.fullmatch(text):
            sound = _sound(text)
            if sound is not None:
                return sound
    parser = AutoLink()
    parser.feed(body)
    parser.close()
    return "".join(parser.parts)
