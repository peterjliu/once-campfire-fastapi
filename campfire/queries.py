"""Shared reads over the Rails tables."""

from . import db
from .models import Account, Blob, Membership, Message, Room, Session, User


def user_by_id(user_id: int) -> User | None:
    row = db.one(f"SELECT {User.COLUMNS} FROM users WHERE id=?", (user_id,))
    return None if row is None else User.load(row)


def users_by_ids(ids: list[int]) -> dict[int, User]:
    if not ids:
        return {}
    unique = list(dict.fromkeys(ids))
    found = db.rows(
        f"SELECT {User.COLUMNS} FROM users WHERE id IN ({db.placeholders(len(unique))})",
        unique,
    )
    return {user.id: user for user in map(User.load, found)}


def session_user(token: str) -> tuple[Session, User] | None:
    row = db.one(
        f"SELECT {Session.COLUMNS},{User.COLUMNS} FROM sessions "
        "JOIN users ON users.id=sessions.user_id WHERE sessions.token=? AND users.status=0",
        (token,),
    )
    return None if row is None else (Session.load(row), User.load(row, 4))


def bot_user(bot_key: str) -> User | None:
    bot_id, separator, token = bot_key.strip().partition("-")
    if not separator or not bot_id.isdigit():
        return None
    row = db.one(
        f"SELECT {User.COLUMNS} FROM users WHERE id=? AND bot_token=? AND status=0 AND role=2",
        (int(bot_id), token),
    )
    return None if row is None else User.load(row)


def account() -> Account | None:
    row = db.one(f"SELECT {Account.COLUMNS} FROM accounts ORDER BY id LIMIT 1")
    return None if row is None else Account.load(row)


def room_by_id(room_id: int) -> Room | None:
    row = db.one(f"SELECT {Room.COLUMNS} FROM rooms WHERE id=?", (room_id,))
    return None if row is None else Room.load(row)


def room_for(user_id: int, room_id: int) -> Room | None:
    """A room is reachable only through an actual membership, even when open."""
    row = db.one(
        f"SELECT {Room.COLUMNS} FROM rooms JOIN memberships ON memberships.room_id=rooms.id "
        "WHERE rooms.id=? AND memberships.user_id=?",
        (room_id, user_id),
    )
    return None if row is None else Room.load(row)


def membership(room_id: int, user_id: int) -> Membership | None:
    row = db.one(
        f"SELECT {Membership.COLUMNS} FROM memberships WHERE room_id=? AND user_id=?",
        (room_id, user_id),
    )
    return None if row is None else Membership.load(row)


def is_member(room_id: int, user_id: int) -> bool:
    return db.exists("SELECT 1 FROM memberships WHERE room_id=? AND user_id=?", (room_id, user_id))


def get_message(message_id: int) -> Message | None:
    row = db.one(f"SELECT {Message.COLUMNS} FROM messages WHERE id=?", (message_id,))
    return None if row is None else Message.load(row)


def blob_for(blob_id: int) -> Blob | None:
    row = db.one(f"SELECT {Blob.COLUMNS} FROM active_storage_blobs WHERE id=?", (blob_id,))
    return None if row is None else Blob.load(row)


def blob_by_key(key: str) -> Blob | None:
    row = db.one(f"SELECT {Blob.COLUMNS} FROM active_storage_blobs WHERE key=?", (key,))
    return None if row is None else Blob.load(row)


def attached_blob(record_type: str, record_id: int, name: str) -> Blob | None:
    row = db.one(
        f"SELECT {Blob.COLUMNS} FROM active_storage_attachments JOIN active_storage_blobs "
        "ON active_storage_blobs.id=active_storage_attachments.blob_id WHERE "
        "record_type=? AND record_id=? AND name=? ORDER BY active_storage_attachments.id LIMIT 1",
        (record_type, record_id, name),
    )
    return None if row is None else Blob.load(row)


def message_attachment_blob(message_id: int) -> Blob | None:
    return attached_blob("Message", message_id, "attachment")


def has_attachment(record_type: str, record_id: int, name: str) -> bool:
    return db.exists(
        "SELECT 1 FROM active_storage_attachments WHERE record_type=? AND record_id=? AND name=?",
        (record_type, record_id, name),
    )


def rich_text_body(message_id: int) -> str | None:
    found = db.value(
        "SELECT body FROM action_text_rich_texts WHERE record_type='Message' "
        "AND record_id=? AND name='body'",
        (message_id,),
    )
    return found if isinstance(found, str) else None
