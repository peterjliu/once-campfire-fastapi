"""Action Text content: sanitizer, Trix canonicalization, plain text and embeds."""

import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import nh3

from . import db, rails
from .jsonvalue import as_dict
from .models import Blob
from .queries import blob_for, user_by_id

TAGS = {
    "a",
    "abbr",
    "b",
    "blockquote",
    "br",
    "caption",
    "code",
    "dd",
    "del",
    "div",
    "dl",
    "dt",
    "em",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "mark",
    "ol",
    "p",
    "pre",
    "s",
    "small",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
    "action-text-attachment",
    "actiontext-opengraph-embed",
    "tfoot",
    "time",
}
ATTRS = {
    "*": {"class", "dir", "lang", "data-language"},
    "a": {"href", "title"},
    "img": {"src", "alt", "width", "height"},
    "action-text-attachment": {
        "sgid",
        "content-type",
        "content",
        "url",
        "href",
        "filename",
        "filesize",
        "width",
        "height",
        "presentation",
        "caption",
    },
}
ATTACHMENT = re.compile(r"<action-text-attachment\b[^>]*>.*?</action-text-attachment>", re.S)
FIGURE = re.compile(r"<figure\b[^>]*>.*?</figure>", re.S)
VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
CLOSES_PARAGRAPH = {"p", "div", "blockquote", "h1", "h2", "h3", "ul", "ol", "pre"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

type Attributes = dict[str, str]


def _trix_figure(match: re.Match[str]) -> str:
    tree = PlainTree()
    tree.feed(match.group(0))
    node = tree.root.children[0] if tree.root.children else None
    if node is None or "data-trix-attachment" not in node.attrs:
        return match.group(0)
    attributes: dict[str, object] = {}
    for name in ("data-trix-attachment", "data-trix-attributes"):
        try:
            loaded = as_dict(json.loads(node.attrs.get(name, "{}")))
        except ValueError, TypeError:
            continue
        if loaded is not None:
            attributes.update(loaded)
    attributes["content-type"] = attributes.pop("contentType", "")
    serialized = " ".join(
        f'{name}="{html.escape(str(value), quote=True)}"'
        for name, value in attributes.items()
        if value is not None
    )
    return f"<action-text-attachment {serialized}></action-text-attachment>"


def canonicalize(body: str | None) -> str:
    """Legacy Trix figures become canonical Action Text attachments."""
    text = body or ""
    if "<figure" not in text:
        return text
    return FIGURE.sub(_trix_figure, text)


def sanitize(body: str | None) -> str:
    return nh3.clean(
        canonicalize(body),
        tags=TAGS,
        attributes=ATTRS,
        url_schemes={"http", "https", "mailto"},
        link_rel=None,
        strip_comments=True,
    )


class AttachmentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.attributes: Attributes = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "action-text-attachment":
            self.attributes = {name: value or "" for name, value in attrs}


def attachment_attributes(node: str) -> Attributes:
    parser = AttachmentParser()
    parser.feed(node)
    return parser.attributes


def attached_blob(attributes: Attributes) -> Blob | None:
    try:
        gid = rails.verify_sgid(attributes.get("sgid", ""))
    except ValueError, TypeError:
        return None
    parsed = urlsplit(gid)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "gid"
        or parsed.netloc != "campfire"
        or len(parts) != 2
        or parts[0] != "ActiveStorage::Blob"
        or not parts[1].isdigit()
    ):
        return None
    return blob_for(int(parts[1]))


class PlainNode:
    __slots__ = ("attrs", "children", "parent", "tag", "text")

    def __init__(
        self,
        tag: str = "",
        attrs: Attributes | None = None,
        parent: PlainNode | None = None,
        text: str = "",
    ) -> None:
        self.tag = tag
        self.attrs: Attributes = attrs or {}
        self.parent = parent
        self.text = text
        self.children: list[PlainNode] = []


class PlainTree(HTMLParser):
    """A bounded fragment tree for Action Text's bottom-up conversion."""

    def __init__(self) -> None:
        super().__init__()
        self.root = PlainNode()
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if len(self.stack) > 200:
            return
        top = self.stack[-1].tag
        if (
            (top == "p" and tag in CLOSES_PARAGRAPH)
            or (tag == "li" and top == "li")
            or (tag in HEADINGS and top in HEADINGS)
        ):
            self.stack.pop()
        node = PlainNode(tag, {name: value or "" for name, value in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                return

    def handle_data(self, data: str) -> None:
        top = self.stack[-1]
        if top.tag in {"pre", "textarea"} and not top.children and data.startswith("\n"):
            data = data[1:]
        top.children.append(PlainNode("#text", parent=top, text=data))


def _attachment_text(node: PlainNode) -> str:
    user_id = rails.unverified_user_gid(node.attrs.get("sgid", ""))
    user = user_by_id(user_id) if user_id else None
    if user is not None:
        return "@" + user.name
    blob = attached_blob(node.attrs)
    if blob is not None:
        return node.attrs.get("caption") or "[" + blob.filename + "]"
    kind = node.attrs.get("content-type", "").split("/")[0].title()
    return "[" + kind + "]" if kind in {"Image", "Video", "Audio"} else ""


def _list_ancestors(node: PlainNode) -> list[str]:
    ancestors: list[str] = []
    parent = node.parent
    while parent is not None:
        if parent.tag in {"ul", "ol"}:
            ancestors.append(parent.tag)
        parent = parent.parent
    return ancestors


def plain_node(node: PlainNode) -> str:
    # Installed actiontext/lib/action_text/plain_text_conversion.rb.
    tag = node.tag
    if tag == "#text":
        return node.text.rstrip("\r\n")
    if tag in {"script", "style", "unsupported"}:
        return ""
    if tag == "action-text-attachment":
        return _attachment_text(node)
    text = "".join(plain_node(child) for child in node.children)
    trimmed = text.rstrip("\r\n")
    if tag in {"p", "h1"}:
        return trimmed + "\n\n"
    if tag in {"ul", "ol"}:
        return ("\n" if _list_ancestors(node) else "") + trimmed + "\n\n"
    if tag == "br":
        return "\n"
    if tag == "div":
        return trimmed + "\n"
    if tag == "figcaption":
        return "[" + trimmed + "]"
    if tag == "blockquote":
        quoted = trimmed + "\n\n"
        content = quoted.strip()
        if not content:
            return "“”"
        start = quoted.index(content)
        return quoted[:start] + "“" + content + "”" + quoted[start + len(content) :]
    if tag == "li":
        ancestors = _list_ancestors(node)
        bullet = "•"
        if ancestors and ancestors[0] == "ol" and node.parent is not None:
            elements = [child for child in node.parent.children if child.tag != "#text"]
            bullet = str(elements.index(node) + 1) + "."
        return "  " * max(0, len(ancestors) - 1) + bullet + " " + trimmed + "\n"
    return text


def plain_text(body: str | None) -> str:
    tree = PlainTree()
    tree.feed(
        canonicalize(body)
        .strip(" \t\n\v\f\r\0")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\0", "")
    )
    return plain_node(tree.root).rstrip("\r\n")


def embedded_blobs(body: str) -> list[Blob]:
    return [
        blob
        for node in ATTACHMENT.findall(body)
        if (blob := attached_blob(attachment_attributes(node))) is not None
    ]


def reconcile_embeds(rich_text_id: int, body: str) -> None:
    """Persist signed inline blobs as rich-text embeds; purge obsolete ones after commit."""
    from . import jobs

    tx = db.current()
    assert tx is not None, "embeds reconcile inside the message transaction"
    blobs = embedded_blobs(body)
    ids = {blob.id for blob in blobs}
    existing = [
        int(row[0])
        for row in db.rows(
            "SELECT blob_id FROM active_storage_attachments WHERE "
            "record_type='ActionText::RichText' AND record_id=? AND name='embeds'",
            (rich_text_id,),
        )
    ]
    obsolete = [blob_id for blob_id in existing if blob_id not in ids]
    for blob_id in obsolete:
        db.run(
            "DELETE FROM active_storage_attachments WHERE record_type='ActionText::RichText' "
            "AND record_id=? AND name='embeds' AND blob_id=?",
            (rich_text_id, blob_id),
        )
    from .clock import format_time, now

    for blob in blobs:
        if blob.id not in existing:
            db.run(
                "INSERT OR IGNORE INTO active_storage_attachments"
                "(blob_id,created_at,name,record_id,record_type) VALUES (?,?,?,?,?)",
                (blob.id, format_time(now()), "embeds", rich_text_id, "ActionText::RichText"),
            )
    for blob_id in obsolete:
        tx.on_commit(lambda blob_id=blob_id: jobs.enqueue("purge", {"blob_id": blob_id}))


SGID_ATTRIBUTE = re.compile(r'sgid="([^"]+)"')


def mention_ids(body: str | None) -> list[int]:
    # reference/lib/rails_ext/action_text_attachables.rb deliberately retains
    # User mentions across secret rotation; unsigned blobs are never accepted.
    if not body or "sgid=" not in body:
        return []
    return [
        user_id
        for raw in SGID_ATTRIBUTE.findall(body)
        if (user_id := rails.unverified_user_gid(html.unescape(raw))) is not None
        and user_by_id(user_id) is not None
    ]
