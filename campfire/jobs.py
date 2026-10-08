"""Leased SQLite jobs survive restarts independently of the Rails application schema."""

import json
import logging
import mimetypes
import secrets
import sqlite3
import threading
import time
from collections.abc import Generator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass

import httpx

from . import config, db
from .jsonvalue import as_dict
from .push import Gone, permitted_endpoint, send

__all__ = ["permitted_endpoint"]

LEASE_SECONDS = 120
MAX_ATTEMPTS = 5
log = logging.getLogger("campfire.jobs")
_stop = threading.Event()
_thread: threading.Thread | None = None
_rates: dict[str, list[float]] = {}
_rates_lock = threading.Lock()

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,"
    "attempts INTEGER NOT NULL DEFAULT 0,available_at REAL NOT NULL,lease_until REAL,"
    "lease_token TEXT,status TEXT NOT NULL DEFAULT 'ready',last_error TEXT)"
)

type JobData = dict[str, object]


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    payload: str
    attempts: int
    token: str


@contextmanager
def connect() -> Generator[sqlite3.Connection]:
    path = config.settings.storage_path / "db/jobs.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10, isolation_level=None)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(SCHEMA)
        yield connection


def reset_rates() -> None:
    with _rates_lock:
        _rates.clear()


def rate_limit(key: str, count: int, seconds: int) -> bool:
    from .cable import redis_client

    client = redis_client()
    if client is not None:
        script = (
            "local n=redis.call('INCR',KEYS[1]);"
            "if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end;return n"
        )
        result = client.eval(script, 1, "campfire:limit:" + key, seconds)
        return isinstance(result, int) and result <= count
    current = time.time()
    with _rates_lock:
        times = [t for t in _rates.get(key, []) if t > current - seconds]
        times.append(current)
        _rates[key] = times
        return len(times) <= count


def enqueue(kind: str, data: JobData) -> None:
    enqueue_many([(kind, data)])


def enqueue_many(jobs: Sequence[tuple[str, JobData]]) -> None:
    if not jobs:
        return
    available_at = time.time()
    rows = [(json.dumps({"kind": kind, "data": data}), available_at) for kind, data in jobs]
    # Individual leased/retried jobs, admitted in one durable queue transaction.
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany("INSERT INTO jobs(payload,available_at) VALUES (?,?)", rows)
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")


def claim() -> Job | None:
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = time.time()
        row: tuple[int, str, int] | None = connection.execute(
            "SELECT id,payload,attempts FROM jobs WHERE status='ready' AND available_at<=? "
            "AND (lease_until IS NULL OR lease_until<=?) ORDER BY id LIMIT 1",
            (current, current),
        ).fetchone()
        if row is None:
            connection.execute("COMMIT")
            return None
        token = secrets.token_hex(16)
        connection.execute(
            "UPDATE jobs SET lease_until=?,lease_token=?,attempts=attempts+1 WHERE id=?",
            (current + LEASE_SECONDS, token, row[0]),
        )
        connection.execute("COMMIT")
        return Job(row[0], row[1], row[2] + 1, token)


def finish(job: Job, error: BaseException | None = None) -> None:
    """Fenced by the lease token: a stale worker cannot complete a reclaimed job."""
    with connect() as connection:
        if error is None:
            connection.execute("DELETE FROM jobs WHERE id=? AND lease_token=?", (job.id, job.token))
            return
        connection.execute(
            "UPDATE jobs SET lease_until=NULL,lease_token=NULL,available_at=?,status=?,"
            "last_error=? WHERE id=? AND lease_token=?",
            (
                time.time() + min(300, 2**job.attempts),
                "dead" if job.attempts >= MAX_ATTEMPTS else "ready",
                str(error)[:1000],
                job.id,
                job.token,
            ),
        )


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=work, name="campfire-jobs", daemon=True)
    _thread.start()


def stop() -> None:
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=10)
    _thread = None


def work() -> None:
    while not _stop.is_set():
        job: Job | None = None
        try:
            job = claim()
            if job is None:
                _stop.wait(0.25)
                continue
            payload = as_dict(json.loads(job.payload)) or {}
            perform(str(payload.get("kind")), as_dict(payload.get("data")) or {})
            finish(job)
        except Exception as error:
            log.exception("Campfire job failed")
            if job is not None:
                finish(job, error)
            else:
                _stop.wait(1)


def _int(data: JobData, key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"job field {key} must be an integer")
    return value


def perform(kind: str, data: JobData) -> None:
    from . import messaging
    from .queries import get_message, rich_text_body
    from .storage import purge_blob

    if kind == "purge":
        purge_blob(_int(data, "blob_id"))
        return
    if kind == "ban-content":
        messaging.delete_messages_by(_int(data, "user_id"))
        return
    message = get_message(_int(data, "message_id"))
    if message is None:
        return
    body = rich_text_body(message.id) or ""
    if kind == "webhook":
        deliver_webhook(_int(data, "webhook_id"), message.id, body)
    elif kind == "push":
        deliver_push(_int(data, "user_id"), message.id, body)


def deliver_webhook(webhook_id: int, message_id: int, body: str) -> None:
    from . import messaging
    from .models import Room, User
    from .queries import get_message, is_member, room_by_id, user_by_id
    from .richtext import plain_text
    from .storage import Upload

    row = db.one(
        "SELECT webhooks.url,webhooks.user_id FROM webhooks JOIN users ON "
        "users.id=webhooks.user_id WHERE webhooks.id=? AND users.status=0",
        (webhook_id,),
    )
    message = get_message(message_id)
    if row is None or message is None:
        return
    bot: User | None = user_by_id(int(row[1]))
    room: Room | None = room_by_id(message.room_id)
    creator = user_by_id(message.creator_id)
    if bot is None or room is None or creator is None or not is_member(room.id, bot.id):
        return
    payload = {
        "user": {"id": message.creator_id, "name": creator.name},
        "room": {
            "id": room.id,
            "name": room.name,
            "path": f"/rooms/{room.id}/{bot.id}-{bot.bot_token}/messages",
        },
        "message": {
            "id": message.id,
            "body": {"html": body, "plain": plain_text(body).replace("@" + bot.name, "").strip()},
            "path": f"/rooms/{room.id}/@{message.id}",
        },
    }
    try:
        # Like Rails' Net::HTTP: one request to the administrator-configured URL, no redirects.
        response = httpx.post(str(row[0]), json=payload, timeout=7, follow_redirects=False)
    except httpx.TimeoutException:
        messaging.create_message(bot, room, "Failed to respond within 7 seconds", webhooks=False)
        return
    if response.status_code != 200:
        return
    content_type = response.headers.get("content-type", "").split(";")[0]
    if content_type in ("text/plain", "text/html"):
        messaging.create_message(bot, room, response.text, webhooks=False)
    elif content_type and response.content:
        extension = mimetypes.guess_extension(content_type) or ".bin"
        upload = Upload("attachment" + extension, content_type, response.content)
        messaging.create_message(bot, room, attachment=upload, webhooks=False)


def deliver_push(user_id: int, message_id: int, body: str) -> None:
    from .models import PushSubscription
    from .queries import get_message, room_by_id, user_by_id
    from .richtext import plain_text

    if not config.settings.vapid_private_key:
        return
    message = get_message(message_id)
    room = room_by_id(message.room_id) if message is not None else None
    creator = user_by_id(message.creator_id) if message is not None else None
    if message is None or room is None or creator is None:
        return
    text = plain_text(body)
    direct = room.direct
    title = creator.name if direct else room.name or ""
    summary = text if direct else creator.name + ": " + text
    badge_value = db.value(
        "SELECT count(*) FROM memberships WHERE user_id=? AND unread_at IS NOT NULL", (user_id,)
    )
    badge = badge_value if isinstance(badge_value, int) else 0
    subscriptions = [
        PushSubscription.load(row)
        for row in db.rows(
            f"SELECT {PushSubscription.COLUMNS} FROM push_subscriptions WHERE user_id=?",
            (user_id,),
        )
    ]
    for subscription in subscriptions:
        try:
            send(
                subscription.endpoint or "",
                subscription.p256dh_key or "",
                subscription.auth_key or "",
                title,
                summary,
                f"/rooms/{room.id}",
                badge,
            )
        except Gone:
            db.write(
                lambda subscription=subscription: db.run(
                    "DELETE FROM push_subscriptions WHERE id=?", (subscription.id,)
                )
            )
