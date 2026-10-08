"""Template view models (Go-style field names shared with the retained templates)."""

import html
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from markupsafe import Markup

from . import db
from .assets import asset
from .clock import now, parse_time
from .jsonvalue import as_dict
from .models import ADMINISTRATOR, DIRECT, Blob, Message, Room, User
from .queries import users_by_ids
from .richtext import plain_text
from .richtext_render import render_body

WORD = re.compile(r"\w")


def all_emoji(text: object) -> bool:
    return isinstance(text, str) and bool(text) and not WORD.search(text)


@dataclass(slots=True)
class UserView:
    ID: int
    Role: int
    Name: str
    UpdatedAt: datetime
    Email: str = ""
    Bio: str = ""
    Title: str = ""
    Status: int = 0
    BotKey: str = ""
    WebhookURL: str = ""
    Administer: bool = False


def user_view(user: User | None) -> UserView:
    if user is None:
        return UserView(0, 0, "", now())
    return UserView(
        ID=user.id,
        Role=user.role,
        Name=user.name,
        UpdatedAt=user.updated_at,
        Email=user.email_address or "",
        Bio=user.bio or "",
        Title=user.title,
        Status=user.status,
        BotKey=f"{user.id}-{user.bot_token}",
        Administer=user.role == ADMINISTRATOR,
    )


@dataclass(slots=True)
class RoomView:
    ID: int
    Name: str
    Type: str
    UpdatedAt: datetime | None = None
    CreatorID: int = 0
    Noun: str = "room"
    EditPath: str = ""
    Members: list[UserView] = field(default_factory=list[UserView])
    Label: str = ""
    Unread: bool = False
    Involvement: str = ""
    kind: str = ""

    def DOM(self, prefix: str) -> str:
        return f"{prefix}_rooms_{self.kind}_{self.ID}" if self.kind else ""


def direct_members(room_ids: Sequence[int]) -> dict[int, list[User]]:
    if not room_ids:
        return {}
    found = db.rows(
        f"SELECT memberships.room_id,{User.COLUMNS} FROM memberships JOIN users "
        f"ON users.id=memberships.user_id WHERE memberships.room_id IN "
        f"({db.placeholders(len(room_ids))}) ORDER BY users.name",
        list(room_ids),
    )
    members: dict[int, list[User]] = {room_id: [] for room_id in room_ids}
    for row in found:
        members[int(row[0])].append(User.load(row, 1))
    return members


def room_view(
    room: Room, viewer: User | None = None, members: list[User] | None = None
) -> RoomView:
    kind = room.kind
    others: list[User] = []
    if room.type == DIRECT:
        if members is None:
            members = direct_members([room.id])[room.id]
        others = [u for u in members if viewer is None or u.id != viewer.id]
    name = ", ".join(u.name for u in others) if room.type == DIRECT else room.name
    return RoomView(
        ID=room.id,
        Name=name or "",
        Type=room.type,
        UpdatedAt=room.updated_at,
        CreatorID=room.creator_id,
        Noun="ping" if kind == "direct" else "room",
        EditPath=f"/rooms/{kind}s/{room.id}/edit",
        Members=[user_view(u) for u in others],
        Label=", ".join(u.name.split()[0] for u in others if u.name.split()),
        kind=kind,
    )


def room_views(rooms: Sequence[Room], viewer: User) -> list[RoomView]:
    members = direct_members([room.id for room in rooms if room.type == DIRECT])
    return [room_view(room, viewer, members.get(room.id)) for room in rooms]


@dataclass(slots=True)
class BoostView:
    ID: int
    MessageID: int
    BoosterID: int
    Booster: str
    BoosterTitle: str
    BoosterUpdatedAt: datetime
    Content: str


@dataclass(frozen=True, slots=True)
class AttachmentView:
    Filename: str


@dataclass(slots=True)
class MessageView:
    ID: int
    ClientID: str
    CreatorID: int
    Creator: str
    CreatorTitle: str
    CreatorUpdatedAt: datetime
    RoomID: int
    RoomName: str
    CreatedAt: datetime
    UpdatedAt: datetime
    HTML: Markup
    AllEmoji: bool
    Boosts: list[BoostView]
    Attachment: AttachmentView | None
    DownloadURL: str
    BlobURL: str
    Permalink: str


def load_bodies(ids: Sequence[int]) -> dict[int, str]:
    if not ids:
        return {}
    found = db.rows(
        "SELECT record_id,body FROM action_text_rich_texts WHERE record_type='Message' "
        f"AND name='body' AND record_id IN ({db.placeholders(len(ids))})",
        list(ids),
    )
    return {int(row[0]): row[1] or "" for row in found}


def load_attachments(ids: Sequence[int]) -> dict[int, Blob]:
    if not ids:
        return {}
    found = db.rows(
        f"SELECT active_storage_attachments.record_id,{Blob.COLUMNS} FROM "
        "active_storage_attachments JOIN active_storage_blobs ON "
        "active_storage_blobs.id=active_storage_attachments.blob_id WHERE "
        "record_type='Message' AND name='attachment' AND record_id IN "
        f"({db.placeholders(len(ids))})",
        list(ids),
    )
    return {int(row[0]): Blob.load(row, 1) for row in found}


def load_boosts(ids: Sequence[int]) -> dict[int, list[BoostView]]:
    if not ids:
        return {}
    found = db.rows(
        f"SELECT boosts.id,boosts.message_id,boosts.content,{User.COLUMNS} FROM boosts "
        "JOIN users ON users.id=boosts.booster_id WHERE boosts.message_id IN "
        f"({db.placeholders(len(ids))}) ORDER BY boosts.created_at",
        list(ids),
    )
    boosts: dict[int, list[BoostView]] = {}
    for row in found:
        booster = User.load(row, 3)
        boosts.setdefault(int(row[1]), []).append(
            BoostView(
                ID=row[0],
                MessageID=row[1],
                BoosterID=booster.id,
                Booster=booster.name,
                BoosterTitle=booster.title,
                BoosterUpdatedAt=booster.updated_at,
                Content=row[2],
            )
        )
    return boosts


def room_names(ids: Sequence[int]) -> dict[int, str]:
    unique = list(dict.fromkeys(ids))
    if not unique:
        return {}
    found = db.rows(
        f"SELECT id,name FROM rooms WHERE id IN ({db.placeholders(len(unique))})", unique
    )
    return {int(row[0]): row[1] or "" for row in found}


def attachment_html(blob: Blob) -> str:
    from .media import VARIABLE_TYPES, representation_url
    from .storage import blob_url

    url = blob_url(blob)
    filename = html.escape(blob.filename)
    content_type = blob.content_type or ""
    download = url + "?disposition=attachment"
    if (
        content_type in VARIABLE_TYPES
        or content_type.startswith("video/")
        or content_type == "application/pdf"
    ):
        try:
            metadata = as_dict(json.loads(blob.metadata or "{}")) or {}
        except ValueError:
            metadata = {}
        width, height = metadata.get("width"), metadata.get("height")
        style = ""
        if (
            isinstance(width, (int, float))
            and isinstance(height, (int, float))
            and width
            and height
        ):
            factor = min(1, 1200 / width, 800 / height)
            width, height = width * factor, height * factor
            style = f' style="width: {width / 2}px; aspect-ratio: {width / height};"'
        if content_type.startswith("video/"):
            media = (
                f'<video src="{url}" poster="{representation_url(blob, format="webp")}" '
                'controls preload="none" width="100%" height="100%" '
                'class="message__attachment"></video>'
            )
        else:
            media = (
                f'<a href="{url}" class="flex" data-lightbox-target="image" '
                f'data-action="lightbox#open" data-lightbox-url-value="{download}">'
                f'<img class="message__attachment" src="{representation_url(blob)}" '
                f'alt="{filename}" loading="lazy"></a>'
            )
        return f'<div class="max-inline-size center flex overflow-clip"{style}>{media}</div>'
    return (
        '<div class="flex-inline align-center gap-half">'
        f'<img src="{asset("common-file-text.svg")}" width="22" height="22" '
        f'class="colorize--black" aria-hidden="true"><span>{filename}</span>'
        f'<a class="btn message__action-btn hide-in-ios-pwa" href="{download}">'
        f'<img src="{asset("download.svg")}" width="20" height="20" aria-hidden="true">'
        f'<span class="for-screen-reader">Download {filename}</span></a>'
        '<button class="btn message__action-btn" data-controller="web-share" '
        f'data-action="web-share#share" data-web-share-files-value="{download}">'
        f'<img src="{asset("share.svg")}" width="20" height="20" aria-hidden="true">'
        f'<span class="for-screen-reader">Share {filename}</span></button></div>'
    )


def message_views(
    messages: Sequence[Message],
    origin: str = "",
    *,
    bodies: dict[int, str] | None = None,
    attachments: dict[int, Blob] | None = None,
    boosts: dict[int, list[BoostView]] | None = None,
    creators: dict[int, User] | None = None,
) -> list[MessageView]:
    from .storage import blob_url

    ids = [m.id for m in messages]
    bodies = load_bodies(ids) if bodies is None else bodies
    attachments = load_attachments(ids) if attachments is None else attachments
    boosts = load_boosts(ids) if boosts is None else boosts
    creators = users_by_ids([m.creator_id for m in messages]) if creators is None else creators
    names = room_names([m.room_id for m in messages])
    host = (urlsplit(origin).hostname or "") if origin else ""
    result: list[MessageView] = []
    for m in messages:
        blob = attachments.get(m.id)
        source = bodies.get(m.id, "")
        presented = attachment_html(blob) if blob is not None else render_body(source, host)
        creator = creators.get(m.creator_id)
        url = blob_url(blob) if blob is not None else ""
        result.append(
            MessageView(
                ID=m.id,
                ClientID=m.client_message_id,
                CreatorID=m.creator_id,
                Creator=creator.name if creator else "",
                CreatorTitle=creator.title if creator else "",
                CreatorUpdatedAt=creator.updated_at if creator else parse_time("1970-01-01"),
                RoomID=m.room_id,
                RoomName=names.get(m.room_id, ""),
                CreatedAt=m.created_at,
                UpdatedAt=m.updated_at,
                HTML=Markup('<div class="lexxy-content">' + presented + "</div>"),
                AllEmoji=all_emoji(plain_text(source)) if source else False,
                Boosts=boosts.get(m.id, []),
                Attachment=AttachmentView(blob.filename) if blob is not None else None,
                DownloadURL=url + "?disposition=attachment" if url else "",
                BlobURL=url,
                Permalink=f"{origin}/rooms/{m.room_id}/@{m.id}",
            )
        )
    return result
