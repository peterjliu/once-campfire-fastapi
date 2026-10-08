"""Typed rows for the unmanaged Rails tables. Column lists keep SELECTs and loaders aligned."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .clock import parse_optional, parse_time

ADMINISTRATOR = 1
BOT = 2
ACTIVE = 0
DEACTIVATED = 1
BANNED = 2
ROLES = ("member", "administrator", "bot")
DIRECT = "Rooms::Direct"
OPEN = "Rooms::Open"
CLOSED = "Rooms::Closed"


@dataclass(slots=True)
class User:
    id: int
    name: str
    email_address: str | None
    password_digest: str | None
    bio: str | None
    bot_token: str | None
    role: int
    status: int
    created_at: datetime
    updated_at: datetime

    COLUMNS = (
        "users.id,users.name,users.email_address,users.password_digest,users.bio,"
        "users.bot_token,users.role,users.status,users.created_at,users.updated_at"
    )

    @staticmethod
    def load(row: sqlite3.Row, offset: int = 0) -> User:
        r = row
        o = offset
        return User(
            r[o],
            r[o + 1],
            r[o + 2],
            r[o + 3],
            r[o + 4],
            r[o + 5],
            r[o + 6],
            r[o + 7],
            parse_time(r[o + 8]),
            parse_time(r[o + 9]),
        )

    @property
    def title(self) -> str:
        return " – ".join(part for part in (self.name, self.bio) if part)

    @property
    def administrator(self) -> bool:
        return self.role == ADMINISTRATOR

    @property
    def bot(self) -> bool:
        return self.role == BOT

    def can_administer(self, creator_id: int | None = None) -> bool:
        return self.role == ADMINISTRATOR or (creator_id is not None and creator_id == self.id)


@dataclass(slots=True)
class Account:
    id: int
    name: str
    join_code: str
    custom_styles: str | None
    settings: str | None
    updated_at: datetime

    COLUMNS = "id,name,join_code,custom_styles,settings,updated_at"

    @staticmethod
    def load(r: sqlite3.Row) -> Account:
        return Account(r[0], r[1], r[2], r[3], r[4], parse_time(r[5]))


@dataclass(slots=True)
class Room:
    id: int
    name: str | None
    type: str
    creator_id: int
    created_at: datetime
    updated_at: datetime

    COLUMNS = "rooms.id,rooms.name,rooms.type,rooms.creator_id,rooms.created_at,rooms.updated_at"

    @staticmethod
    def load(r: sqlite3.Row, o: int = 0) -> Room:
        return Room(r[o], r[o + 1], r[o + 2], r[o + 3], parse_time(r[o + 4]), parse_time(r[o + 5]))

    @property
    def direct(self) -> bool:
        return self.type == DIRECT

    @property
    def kind(self) -> str:
        """`open`, `closed` or `direct`, as used in DOM IDs and paths."""
        return self.type.rsplit("::", 1)[-1].lower()


@dataclass(slots=True)
class Membership:
    id: int
    room_id: int
    user_id: int
    involvement: str
    connected_at: datetime | None
    connections: int
    unread_at: datetime | None

    COLUMNS = (
        "memberships.id,memberships.room_id,memberships.user_id,memberships.involvement,"
        "memberships.connected_at,memberships.connections,memberships.unread_at"
    )

    @staticmethod
    def load(r: sqlite3.Row, o: int = 0) -> Membership:
        return Membership(
            r[o],
            r[o + 1],
            r[o + 2],
            r[o + 3] or "mentions",
            parse_optional(r[o + 4]),
            r[o + 5],
            parse_optional(r[o + 6]),
        )


@dataclass(slots=True)
class Message:
    id: int
    room_id: int
    creator_id: int
    client_message_id: str
    created_at: datetime
    updated_at: datetime
    # The post-commit fragment rendered once for Cable and the HTTP response.
    fragment: str | None = None

    COLUMNS = (
        "messages.id,messages.room_id,messages.creator_id,messages.client_message_id,"
        "messages.created_at,messages.updated_at"
    )

    @staticmethod
    def load(r: sqlite3.Row, o: int = 0) -> Message:
        return Message(
            r[o], r[o + 1], r[o + 2], r[o + 3], parse_time(r[o + 4]), parse_time(r[o + 5])
        )


@dataclass(slots=True)
class Blob:
    id: int
    key: str
    filename: str
    content_type: str | None
    metadata: str | None
    byte_size: int
    checksum: str | None
    created_at: datetime

    COLUMNS = (
        "active_storage_blobs.id,active_storage_blobs.key,active_storage_blobs.filename,"
        "active_storage_blobs.content_type,active_storage_blobs.metadata,"
        "active_storage_blobs.byte_size,active_storage_blobs.checksum,"
        "active_storage_blobs.created_at"
    )

    @staticmethod
    def load(r: sqlite3.Row, o: int = 0) -> Blob:
        return Blob(
            r[o], r[o + 1], r[o + 2], r[o + 3], r[o + 4], r[o + 5], r[o + 6], parse_time(r[o + 7])
        )


@dataclass(slots=True)
class Boost:
    id: int
    message_id: int
    booster_id: int
    content: str
    created_at: datetime

    COLUMNS = "boosts.id,boosts.message_id,boosts.booster_id,boosts.content,boosts.created_at"

    @staticmethod
    def load(r: sqlite3.Row, o: int = 0) -> Boost:
        return Boost(r[o], r[o + 1], r[o + 2], r[o + 3], parse_time(r[o + 4]))


@dataclass(slots=True)
class Session:
    id: int
    user_id: int
    token: str
    last_active_at: datetime

    COLUMNS = "sessions.id,sessions.user_id,sessions.token,sessions.last_active_at"

    @staticmethod
    def load(r: sqlite3.Row, o: int = 0) -> Session:
        return Session(r[o], r[o + 1], r[o + 2], parse_time(r[o + 3]))


@dataclass(slots=True)
class PushSubscription:
    id: int
    user_id: int
    endpoint: str | None
    p256dh_key: str | None
    auth_key: str | None
    user_agent: str | None
    updated_at: datetime

    COLUMNS = "id,user_id,endpoint,p256dh_key,auth_key,user_agent,updated_at"

    @staticmethod
    def load(r: sqlite3.Row) -> PushSubscription:
        return PushSubscription(r[0], r[1], r[2], r[3], r[4], r[5], parse_time(r[6]))
