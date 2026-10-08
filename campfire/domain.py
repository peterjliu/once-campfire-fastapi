"""Transaction owners for accounts, users, rooms and memberships.

Message operations live in `messaging` and are re-exported here as the domain surface.
"""

import secrets
from collections.abc import Sequence

import bcrypt

from . import cable, config, db, rendering
from .clock import format_time, now
from .messaging import (
    create_message,
    delete_message,
    enqueue_notifications,
    insert_bare_message,
    publish_message,
    search_message_ids,
    update_message,
)
from .models import DIRECT, OPEN, Message, Room, User
from .presentation import room_view
from .queries import room_by_id, user_by_id

__all__ = [
    "create_message",
    "delete_message",
    "enqueue_notifications",
    "insert_bare_message",
    "publish_message",
    "search_message_ids",
    "update_message",
]


def password_digest(password: str) -> str:
    return bcrypt.hashpw(
        password.encode()[:72], bcrypt.gensalt(config.settings.bcrypt_rounds)
    ).decode()


def check_password(password: str, digest: str | None) -> bool:
    if not digest:
        return False
    try:
        return bcrypt.checkpw(password.encode()[:72], digest.encode())
    except ValueError:
        return False


def create_account(name: str, join_code: str | None = None) -> int:
    with db.transaction():
        stamp = format_time(now())
        return db.run(
            "INSERT INTO accounts(created_at,join_code,name,settings,singleton_guard,updated_at) "
            "VALUES (?,?,?,?,0,?)",
            (stamp, join_code or secrets.token_urlsafe(18), name, "{}", stamp),
        )


def grant_memberships(room: Room, users: Sequence[User]) -> None:
    involvement = "everything" if room.type == DIRECT else "mentions"
    with db.transaction():
        stamp = format_time(now())
        db.run_many(
            "INSERT OR IGNORE INTO memberships(connections,created_at,involvement,room_id,"
            "updated_at,user_id) VALUES (0,?,?,?,?,?)",
            [(stamp, involvement, room.id, stamp, user.id) for user in users],
        )


def create_user(
    name: str,
    email_address: str | None = None,
    password: str = "",
    role: int = 0,
    bot_token: str | None = None,
) -> User:
    digest = password_digest(password) if password or role != 2 else None
    with db.transaction():
        stamp = format_time(now())
        user_id = db.run(
            "INSERT INTO users(bio,bot_token,created_at,email_address,name,password_digest,"
            "role,status,updated_at) VALUES (NULL,?,?,?,?,?,?,0,?)",
            (bot_token, stamp, email_address, name, digest, role, stamp),
        )
        user = user_by_id(user_id)
        assert user is not None
        for row in db.rows(f"SELECT {Room.COLUMNS} FROM rooms WHERE type=?", (OPEN,)):
            grant_memberships(Room.load(row), [user])
        return user


def create_room(room_type: str, creator: User, name: str | None = None) -> Room:
    with db.transaction():
        stamp = format_time(now())
        room_id = db.run(
            "INSERT INTO rooms(created_at,creator_id,name,type,updated_at) VALUES (?,?,?,?,?)",
            (stamp, creator.id, name, room_type, stamp),
        )
        room = room_by_id(room_id)
        assert room is not None
        return room


def require_room(room_id: int) -> Room:
    room = room_by_id(room_id)
    if room is None:
        raise LookupError(f"room {room_id} not found")
    return room


def create_webhook(user: User, url: str) -> int:
    with db.transaction():
        stamp = format_time(now())
        return db.run(
            "INSERT INTO webhooks(created_at,updated_at,url,user_id) VALUES (?,?,?,?)",
            (stamp, stamp, url, user.id),
        )


def set_webhook(user_id: int, url: str) -> None:
    with db.transaction():
        if not url:
            db.run("DELETE FROM webhooks WHERE user_id=?", (user_id,))
            return
        stamp = format_time(now())
        if db.exists("SELECT 1 FROM webhooks WHERE user_id=?", (user_id,)):
            db.run("UPDATE webhooks SET url=?,updated_at=? WHERE user_id=?", (url, stamp, user_id))
        else:
            db.run(
                "INSERT INTO webhooks(created_at,updated_at,url,user_id) VALUES (?,?,?,?)",
                (stamp, stamp, url, user_id),
            )


def delete_room(room: Room) -> None:
    with db.transaction() as tx:
        for row in db.rows(f"SELECT {Message.COLUMNS} FROM messages WHERE room_id=?", (room.id,)):
            delete_message(Message.load(row))
        db.run("DELETE FROM memberships WHERE room_id=?", (room.id,))
        db.run("DELETE FROM rooms WHERE id=?", (room.id,))
        removal = rendering.turbo_stream("remove", f"list_rooms_{room.kind}_{room.id}")
        tx.on_commit(lambda: cable.publish("rooms", removal))


def deactivate(user: User) -> None:
    with db.transaction():
        db.run(
            "DELETE FROM memberships WHERE user_id=? AND room_id IN "
            "(SELECT id FROM rooms WHERE type<>?)",
            (user.id, DIRECT),
        )
        for table in ("push_subscriptions", "searches", "sessions"):
            db.run(f"DELETE FROM {table} WHERE user_id=?", (user.id,))
        email = user.email_address
        if email:
            email = email.replace("@", "-deactivated-" + secrets.token_hex(8) + "@")
        db.run(
            "UPDATE users SET status=1,email_address=?,updated_at=? WHERE id=?",
            (email, format_time(now()), user.id),
        )


def direct_room_for(user_ids: set[int]) -> int | None:
    """The direct room whose members are exactly `user_ids`, if any."""
    row = db.one(
        "SELECT rooms.id FROM rooms JOIN memberships ON memberships.room_id=rooms.id "
        f"WHERE rooms.type=? GROUP BY rooms.id HAVING count(*)=? AND "
        f"sum(memberships.user_id IN ({db.placeholders(len(user_ids))}))=? ORDER BY rooms.id "
        "LIMIT 1",
        (DIRECT, len(user_ids), *sorted(user_ids), len(user_ids)),
    )
    return None if row is None else int(row[0])


def save_room(
    kind: str, actor: User, room: Room | None, name: str, user_ids: set[int]
) -> tuple[Room, bool]:
    """Create or update a room and its memberships; returns (room, existing direct room)."""
    room_type = "Rooms::" + kind[:-1].capitalize()
    with db.transaction():
        if kind == "directs":
            user_ids = user_ids | {actor.id}
            found = [
                int(row[0])
                for row in db.rows(
                    f"SELECT id FROM users WHERE id IN ({db.placeholders(len(user_ids))})",
                    sorted(user_ids),
                )
            ]
            existing = direct_room_for(set(found))
            if existing is not None:
                return require_room(existing), True
            room = create_room(room_type, actor)
            grant_memberships(room, [u for u in map(user_by_id, found) if u is not None])
            return room, False
        stamp = format_time(now())
        if room is not None:
            db.run(
                "UPDATE rooms SET name=?,type=?,updated_at=? WHERE id=?",
                (name, room_type, stamp, room.id),
            )
            room = require_room(room.id)
        else:
            room = create_room(room_type, actor, name)
        if kind == "opens":
            members = [
                User.load(row)
                for row in db.rows(f"SELECT {User.COLUMNS} FROM users WHERE status=0")
            ]
        else:
            members = [u for u in map(user_by_id, sorted(user_ids)) if u is not None]
        keep = [member.id for member in members]
        db.run(
            f"DELETE FROM memberships WHERE room_id=? AND user_id NOT IN ({db.placeholders(len(keep))})",
            (room.id, *keep),
        )
        grant_memberships(room, members)
        return room, False


def broadcast_room(room: Room, kind: str) -> None:
    """Prepend the room to each member's sidebar (shared rooms use the public stream)."""
    publications: list[cable.Publication] = []
    for row in db.rows(
        f"SELECT {User.COLUMNS} FROM memberships JOIN users ON users.id=memberships.user_id "
        "WHERE memberships.room_id=?",
        (room.id,),
    ):
        member = User.load(row)
        view = room_view(room, member)
        view.Unread = False
        from . import rails

        stream = "rooms" if kind == "opens" else rails.user_rooms_stream(member.id)
        target = "direct_rooms" if kind == "directs" else "shared_rooms"
        fragment = rendering.render(
            "sidebar-direct" if kind == "directs" else "sidebar-shared", view
        )
        publications.append((stream, rendering.turbo_stream("prepend", target, fragment)))
    cable.publish_many(publications)
