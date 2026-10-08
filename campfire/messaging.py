"""Transaction owners for messages and boosts; publications follow the commit."""

import uuid
from datetime import timedelta

from . import cable, db, jobs, media, rails, rendering
from .clock import format_time, now
from .models import DIRECT, Blob, Message, Room, User
from .presentation import message_views
from .queries import message_attachment_blob, room_by_id
from .richtext import mention_ids, plain_text, reconcile_embeds, sanitize
from .storage import Upload, attach_signed, blob_ids, remove_attachment, store_upload

PRESENCE_WINDOW = timedelta(seconds=60)
SEARCH_PROBE = 1000
SEARCH_LIMIT = 100

type AttachmentInput = Upload | str


def search_message_ids(user: User, query: str) -> list[int]:
    """Newest matching IDs first. A bounded reverse FTS walk avoids sorting every match;
    a membership-scoped fallback covers sparse memberships."""
    terms = " ".join('"' + word.replace('"', '""') + '"' for word in query.split())
    if not terms:
        return []
    ids: list[int] = []
    examined = 0
    for row in db.rows(
        "SELECT m.id, ms.user_id IS NOT NULL FROM message_search_index idx "
        "JOIN messages m ON m.id=idx.rowid LEFT JOIN memberships ms "
        "ON ms.room_id=m.room_id AND ms.user_id=? WHERE idx.body MATCH ? "
        f"ORDER BY idx.rowid DESC LIMIT {SEARCH_PROBE}",
        (user.id, terms),
    ):
        examined += 1
        if row[1]:
            ids.append(int(row[0]))
            if len(ids) == SEARCH_LIMIT:
                break
    if len(ids) < SEARCH_LIMIT and examined == SEARCH_PROBE:
        ids = [
            int(row[0])
            for row in db.rows(
                "SELECT m.id FROM messages m JOIN message_search_index idx ON idx.rowid=m.id "
                "JOIN memberships ms ON ms.room_id=m.room_id WHERE ms.user_id=? "
                f"AND idx.body MATCH ? ORDER BY m.id DESC LIMIT {SEARCH_LIMIT}",
                (user.id, terms),
            )
        ]
    return ids


def index_message(message_id: int, text: str, *, fresh: bool = False) -> None:
    if not fresh:
        db.run("DELETE FROM message_search_index WHERE rowid=?", (message_id,))
    db.run("INSERT INTO message_search_index(rowid,body) VALUES (?,?)", (message_id, text))


def _attach(attachment: AttachmentInput, message_id: int) -> Blob:
    if isinstance(attachment, Upload):
        blob = store_upload(attachment, "Message", message_id, "attachment")
    else:
        blob = attach_signed(attachment, "Message", message_id, "attachment")
    media.process_attachment(blob)
    return blob


def insert_bare_message(room: Room, creator: User, client_id: str) -> Message:
    """A message row without content or side effects (imports and tests)."""
    with db.transaction():
        created = now()
        stamp = format_time(created)
        message_id = db.run(
            "INSERT INTO messages(client_message_id,created_at,creator_id,room_id,updated_at) "
            "VALUES (?,?,?,?,?)",
            (client_id, stamp, creator.id, room.id, stamp),
        )
        return Message(message_id, room.id, creator.id, client_id, created, created)


def create_message(
    user: User,
    room: Room,
    body: str = "",
    client_id: str | None = None,
    attachment: AttachmentInput | None = None,
    *,
    webhooks: bool = True,
) -> Message:
    # Pure parsing runs before taking SQLite's single writer lock. Mentions and blobs
    # resolve current records inside the transaction.
    body = sanitize(body)
    has_embeds = "<action-text-attachment" in body.lower()
    text = None if has_embeds else plain_text(body)
    with db.transaction() as tx:
        # Membership is rechecked inside the write transaction, including bot writes.
        if not db.exists(
            "SELECT 1 FROM memberships WHERE user_id=? AND room_id=?", (user.id, room.id)
        ):
            raise PermissionError("room membership required")
        created = now()
        stamp = format_time(created)
        client_message_id = client_id or str(uuid.uuid4())
        message_id = db.run(
            "INSERT INTO messages(client_message_id,created_at,creator_id,room_id,updated_at) "
            "VALUES (?,?,?,?,?)",
            (client_message_id, stamp, user.id, room.id, stamp),
        )
        message = Message(message_id, room.id, user.id, client_message_id, created, created)
        rich_id = db.run(
            "INSERT INTO action_text_rich_texts(body,created_at,name,record_id,record_type,"
            "updated_at) VALUES (?,?,'body',?,'Message',?)",
            (body, stamp, message_id, stamp),
        )
        blob = _attach(attachment, message_id) if attachment else None
        if has_embeds:
            reconcile_embeds(rich_id, body)
            text = plain_text(body)
        index_message(message_id, text or (blob.filename if blob else ""), fresh=True)
        db.run("UPDATE rooms SET updated_at=? WHERE id=?", (stamp, room.id))
        # Shared rooms preserve the first unread message; pings keep the latest.
        first_unread = "" if room.type == DIRECT else " AND unread_at IS NULL"
        db.run(
            "UPDATE memberships SET unread_at=?,updated_at=? WHERE room_id=? AND user_id<>? "
            "AND (involvement IS NULL OR involvement<>'invisible')"
            + first_unread
            + " AND (connected_at IS NULL OR connected_at<?)",
            (stamp, stamp, room.id, user.id, format_time(created - PRESENCE_WINDOW)),
        )

        def created_hook() -> None:
            message.fragment = publish_message(message, "append", body=body, blob=blob, room=room)
            enqueue_notifications(message, body, webhooks=webhooks, room=room)

        tx.on_commit(created_hook)
        return message


def update_message(
    message: Message, body: str | None = None, attachment: AttachmentInput | None = None
) -> None:
    """`attachment=None` keeps the attachment; an empty string removes it."""
    with db.transaction() as tx:
        if body is not None:
            body = sanitize(body)
            stamp = format_time(now())
            db.run(
                "INSERT INTO action_text_rich_texts(body,created_at,name,record_id,record_type,"
                "updated_at) VALUES (?,?,'body',?,'Message',?) ON CONFLICT"
                "(record_type,record_id,name) DO UPDATE SET body=excluded.body,"
                "updated_at=excluded.updated_at",
                (body, stamp, message.id, stamp),
            )
            rich_id = db.value(
                "SELECT id FROM action_text_rich_texts WHERE record_type='Message' "
                "AND record_id=? AND name='body'",
                (message.id,),
            )
            assert isinstance(rich_id, int)
            reconcile_embeds(rich_id, body)
        else:
            from .queries import rich_text_body

            body = rich_text_body(message.id) or ""
        if attachment is not None:
            remove_attachment("Message", message.id, "attachment")
            if attachment:
                _attach(attachment, message.id)
        updated = now()
        message.updated_at = updated
        stamp = format_time(updated)
        db.run("UPDATE messages SET updated_at=? WHERE id=?", (stamp, message.id))
        current = message_attachment_blob(message.id)
        index_message(message.id, plain_text(body) or (current.filename if current else ""))
        db.run("UPDATE rooms SET updated_at=? WHERE id=?", (stamp, message.room_id))
        tx.on_commit(lambda: publish_message(message, "replace"))


def delete_message(message: Message) -> None:
    with db.transaction() as tx:
        db.run("DELETE FROM boosts WHERE message_id=?", (message.id,))
        rich_ids = [
            int(row[0])
            for row in db.rows(
                "SELECT id FROM action_text_rich_texts WHERE record_type='Message' AND record_id=?",
                (message.id,),
            )
        ]
        purged = blob_ids("Message", message.id)
        db.run(
            "DELETE FROM active_storage_attachments WHERE record_type='Message' AND record_id=?",
            (message.id,),
        )
        for rich_id in rich_ids:
            purged += blob_ids("ActionText::RichText", rich_id)
            db.run(
                "DELETE FROM active_storage_attachments WHERE "
                "record_type='ActionText::RichText' AND record_id=?",
                (rich_id,),
            )
        for blob_id in purged:
            tx.on_commit(lambda blob_id=blob_id: jobs.enqueue("purge", {"blob_id": blob_id}))
        db.run(
            "DELETE FROM action_text_rich_texts WHERE record_type='Message' AND record_id=?",
            (message.id,),
        )
        db.run("DELETE FROM message_search_index WHERE rowid=?", (message.id,))
        db.run("DELETE FROM messages WHERE id=?", (message.id,))
        db.run("UPDATE rooms SET updated_at=? WHERE id=?", (format_time(now()), message.room_id))
        room = room_by_id(message.room_id)
        if room is not None:
            tx.on_commit(lambda: publish_message(message, "remove", room=room))


def delete_messages_by(user_id: int) -> None:
    for row in db.rows(f"SELECT {Message.COLUMNS} FROM messages WHERE creator_id=?", (user_id,)):
        delete_message(Message.load(row))


def room_target(room: Room) -> str:
    return f"messages_rooms_{room.kind}_{room.id}"


def publish_message(
    message: Message,
    action: str,
    *,
    body: str | None = None,
    blob: Blob | None = None,
    room: Room | None = None,
) -> str:
    """Render once; the same fragment serves Cable subscribers and the HTTP response."""
    room = room or room_by_id(message.room_id)
    if room is None:
        return ""
    if action == "remove":
        fragment = ""
        target = "message_" + message.client_message_id
    else:
        if body is None:
            view = message_views([message])[0]
        else:
            attachments = {message.id: blob} if blob is not None else {}
            view = message_views(
                [message], bodies={message.id: body}, attachments=attachments, boosts={}
            )[0]
        fragment = rendering.render("message", view)
        target = room_target(room) if action == "append" else "message_" + message.client_message_id
    publications: list[cable.Publication] = [
        (rails.stream_name(room.type, room.id), rendering.turbo_stream(action, target, fragment))
    ]
    if action == "append":
        publications.extend(
            (f"user_{int(row[0])}_unreads", {"roomId": room.id})
            for row in db.rows("SELECT user_id FROM memberships WHERE room_id=?", (room.id,))
        )
    cable.publish_many(publications)
    return fragment


def enqueue_notifications(
    message: Message, body: str, *, webhooks: bool = True, room: Room | None = None
) -> None:
    room = room or room_by_id(message.room_id)
    if room is None:
        return
    pending: list[tuple[str, jobs.JobData]] = []
    mentions = mention_ids(body)
    if webhooks:
        bot_ids = [
            int(row[0])
            for row in db.rows(
                "SELECT memberships.user_id FROM memberships JOIN users ON "
                "users.id=memberships.user_id WHERE memberships.room_id=? AND users.role=2 "
                "AND users.status=0 AND memberships.user_id<>?",
                (room.id, message.creator_id),
            )
        ]
        if room.type != DIRECT:
            bot_ids = [bot_id for bot_id in bot_ids if bot_id in mentions]
        if bot_ids:
            pending.extend(
                ("webhook", {"webhook_id": int(row[0]), "message_id": message.id})
                for row in db.rows(
                    f"SELECT id FROM webhooks WHERE user_id IN ({db.placeholders(len(bot_ids))}) "
                    "ORDER BY id",
                    bot_ids,
                )
            )
    cutoff = format_time(now() - PRESENCE_WINDOW)
    for row in db.rows(
        "SELECT memberships.user_id,memberships.involvement FROM memberships JOIN users ON "
        "users.id=memberships.user_id WHERE memberships.room_id=? AND users.status=0 "
        "AND memberships.user_id<>? AND (memberships.connected_at IS NULL OR "
        "memberships.connected_at<?) ORDER BY memberships.id",
        (room.id, message.creator_id, cutoff),
    ):
        user_id, involvement = int(row[0]), row[1]
        if involvement == "everything" or (involvement == "mentions" and user_id in mentions):
            pending.append(("push", {"user_id": user_id, "message_id": message.id}))
    jobs.enqueue_many(pending)


def boost_target(message: Message) -> str:
    return "boosts_message_" + message.client_message_id


def create_boost(message: Message, booster: User, content: str, *, scroll: bool) -> int:
    with db.transaction() as tx:
        stamp = format_time(now())
        boost_id = db.run(
            "INSERT INTO boosts(booster_id,content,created_at,message_id,updated_at) "
            "VALUES (?,?,?,?,?)",
            (booster.id, content, stamp, message.id, stamp),
        )
        db.run("UPDATE messages SET updated_at=? WHERE id=?", (stamp, message.id))

        def published() -> None:
            room = room_by_id(message.room_id)
            view = message_views([message])[0]
            boost = next(b for b in view.Boosts if boost_id == b.ID)
            extra = ' maintain_scroll="true"' if scroll else ""
            if room is not None:
                cable.publish(
                    rails.stream_name(room.type, room.id),
                    rendering.turbo_stream(
                        "append", boost_target(message), rendering.render("boost", boost), extra
                    ),
                )

        tx.on_commit(published)
        return boost_id


def delete_boost(message: Message, boost_id: int, *, touch: bool = True) -> None:
    with db.transaction() as tx:
        db.run("DELETE FROM boosts WHERE id=?", (boost_id,))
        if touch:
            db.run("UPDATE messages SET updated_at=? WHERE id=?", (format_time(now()), message.id))
        room = room_by_id(message.room_id)
        if room is not None:
            stream = rails.stream_name(room.type, room.id)
            removal = rendering.turbo_stream("remove", f"boost_{boost_id}")
            tx.on_commit(lambda: cable.publish(stream, removal))
