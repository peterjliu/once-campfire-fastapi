"""Action Cable wire protocol with per-publication access checks and shared Redis pub/sub."""

import asyncio
import json
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

import orjson
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from . import bus, config, db, rails
from .clock import format_time, now, parse_time
from .jsonvalue import as_dict
from .middleware import parse_cookies
from .queries import session_user

CHANNEL = "campfire:cable"
PROTOCOL = "actioncable-v1-json"
ROOM_TYPES = ("Room", "Rooms::Open", "Rooms::Closed", "Rooms::Direct")
PRESENCE_WINDOW = 60

type Publication = tuple[str, object]


@dataclass(frozen=True, slots=True)
class Event:
    stream: str
    message: bytes  # JSON-encoded once, shared by every subscriber


@dataclass(eq=False, slots=True)
class Client:
    queue: asyncio.Queue[Event] = field(default_factory=lambda: asyncio.Queue[Event](maxsize=256))
    closed: bool = False


_clients: set[Client] = set()
_loop: asyncio.AbstractEventLoop | None = None
_redis: bus.Client | None = None
_redis_lock = threading.Lock()


def redis_client() -> bus.Client | None:
    global _redis
    url = config.settings.redis_url
    if not url:
        return None
    with _redis_lock:
        if _redis is None:
            _redis = bus.connect(url)
        return _redis


def encode(stream: str, message: object) -> bytes:
    return orjson.dumps({"stream": stream, "message": message})


def publish(stream: str, message: object) -> None:
    publish_many([(stream, message)])


def publish_many(publications: Sequence[Publication]) -> None:
    """Ordered publications; with Redis they share one pipeline round trip."""
    if not publications:
        return
    payloads = [encode(stream, message) for stream, message in publications]
    client = redis_client()
    if client is not None:
        bus.publish_all(client, CHANNEL, payloads)
        return
    loop = _loop
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(deliver_many, payloads)


def deliver_many(payloads: Sequence[bytes]) -> None:
    for payload in payloads:
        deliver(payload)


def deliver(payload: bytes) -> None:
    decoded = as_dict(orjson.loads(payload))
    if decoded is None or not isinstance(decoded.get("stream"), str):
        return
    event = Event(str(decoded["stream"]), orjson.dumps(decoded.get("message")))
    for client in list(_clients):
        try:
            client.queue.put_nowait(event)
        except asyncio.QueueFull:
            client.closed = True


async def relay() -> None:
    """Deliver every worker's publications to this worker's sockets."""
    async for payload in bus.messages(config.settings.redis_url, CHANNEL):
        deliver(payload)


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


@dataclass(frozen=True, slots=True)
class Identity:
    session_id: int
    user_id: int
    user_name: str


@dataclass(frozen=True, slots=True)
class Subscription:
    channel: str
    room: int
    stream: str


def authenticate(cookie_header: str) -> Identity | None:
    raw = parse_cookies(cookie_header).get("session_token")
    if not raw:
        return None
    try:
        token = rails.verify_cookie("session_token", raw)
    except ValueError, TypeError:
        return None
    found = session_user(token) if isinstance(token, str) else None
    if found is None or found[1].bot:
        return None
    session, user = found
    return Identity(session.id, user.id, user.name)


def session_alive(identity: Identity) -> bool:
    return db.exists(
        "SELECT 1 FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.id=? "
        "AND sessions.user_id=? AND users.status=0",
        (identity.session_id, identity.user_id),
    )


def _room_stream(params: dict[str, object], user_id: int) -> Subscription | None:
    stream = rails.verify_stream(str(params.get("signed_stream_name", "")))
    if not isinstance(stream, str) or ":" not in stream:
        return None
    gid, suffix = stream.split(":", 1)
    if suffix != "messages":
        return None
    parts = rails.decode64(gid).decode().split("/")
    if len(parts) != 5 or parts[:3] != ["gid:", "", "campfire"] or parts[3] not in ROOM_TYPES:
        return None
    if not parts[4].isdigit():
        return None
    room_id = int(parts[4])
    room_type = db.value(
        "SELECT rooms.type FROM rooms JOIN memberships ON memberships.room_id=rooms.id "
        "WHERE rooms.id=? AND memberships.user_id=?",
        (room_id, user_id),
    )
    if room_type is None or parts[3] not in ("Room", room_type):
        return None
    return Subscription("RoomMessagesChannel", room_id, stream)


def authorize(identity: Identity, identifier: str) -> Subscription | None:
    """Fresh session and membership checks for a subscription or one publication."""
    if not session_alive(identity):
        return None
    try:
        params = as_dict(json.loads(identifier))
        if params is None:
            return None
        raw_channel = params.get("channel")
        channel = raw_channel if isinstance(raw_channel, str) else ""
        user_id = identity.user_id
        if channel in ("ApplicationCable::Channel", "HeartbeatChannel"):
            return Subscription(channel, 0, "")
        if channel in ("ReadRoomsChannel", "UnreadRoomsChannel"):
            suffix = "reads" if channel == "ReadRoomsChannel" else "unreads"
            return Subscription(channel, 0, f"user_{user_id}_{suffix}")
        if channel == "RoomMessagesChannel":
            return _room_stream(params, user_id)
        if channel in ("RoomChannel", "PresenceChannel", "TypingNotificationsChannel"):
            raw_room = params.get("room_id", 0)
            room_id = int(raw_room) if isinstance(raw_room, (int, str)) else 0
            if not db.exists(
                "SELECT 1 FROM memberships WHERE room_id=? AND user_id=?", (room_id, user_id)
            ):
                return None
            return Subscription(channel, room_id, f"{channel}:{room_id}")
        if channel == "Turbo::StreamsChannel":
            stream = rails.verify_stream(str(params.get("signed_stream_name", "")))
            if stream not in ("rooms", rails.user_rooms_stream(user_id)):
                return None
            return Subscription(channel, 0, str(stream))
    except ValueError, TypeError, KeyError, UnicodeError:
        return None
    return None


def presence(user_id: int, room_id: int, action: str) -> None:
    with db.transaction() as tx:
        row = db.one(
            "SELECT id,connected_at,connections FROM memberships WHERE room_id=? AND user_id=?",
            (room_id, user_id),
        )
        if row is None:
            return
        current = now()
        connected: str | None = row[1]
        active = connected is not None and parse_time(connected) >= current - timedelta(
            seconds=PRESENCE_WINDOW
        )
        stamp = format_time(current)
        if action in ("present", "refresh"):
            count = (row[2] + 1 if action == "present" else row[2]) if active else 1
            db.run(
                "UPDATE memberships SET connections=?,connected_at=?,unread_at=NULL,updated_at=? "
                "WHERE id=?",
                (count, stamp, stamp, row[0]),
            )
            if action == "present":
                tx.on_commit(lambda: publish(f"user_{user_id}_reads", {"room_id": room_id}))
        else:
            count = max(0, row[2] - 1) if active else 0
            db.run(
                "UPDATE memberships SET connections=?,connected_at=?,updated_at=? WHERE id=?",
                (count, connected if count else None, stamp, row[0]),
            )


def frame(value: object) -> str:
    return orjson.dumps(value).decode()


def same_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    parsed = urlsplit(origin)
    expected = "https" if websocket.scope.get("scheme") == "wss" else "http"
    return parsed.netloc == websocket.headers.get("host", "") and parsed.scheme == expected


async def endpoint(websocket: WebSocket) -> None:
    bind_loop(asyncio.get_running_loop())
    if not same_origin(websocket):
        await websocket.close(code=1008)
        return
    identity = authenticate(websocket.headers.get("cookie", ""))
    if identity is None:
        await websocket.close(code=1008)
        return
    if PROTOCOL not in websocket.scope.get("subprotocols", []):
        await websocket.close(code=1002)
        return
    await websocket.accept(subprotocol=PROTOCOL)
    await Connection(websocket, identity).run()


class Connection:
    def __init__(self, websocket: WebSocket, identity: Identity) -> None:
        self.websocket = websocket
        self.identity = identity
        self.client = Client()
        self.subscriptions: dict[str, Subscription] = {}

    async def send(self, text: str) -> None:
        await self.websocket.send_text(text)

    async def pump(self) -> None:
        while not self.client.closed:
            try:
                event = await asyncio.wait_for(self.client.queue.get(), timeout=3)
            except TimeoutError:
                if not session_alive(self.identity):
                    await self.send(
                        frame({"type": "disconnect", "reason": "unauthorized", "reconnect": False})
                    )
                    self.client.closed = True
                    break
                await self.send(frame({"type": "ping", "message": int(time.time())}))
                continue
            for identifier, subscription in list(self.subscriptions.items()):
                if subscription.stream != event.stream:
                    continue
                if authorize(self.identity, identifier) is None:
                    self.subscriptions.pop(identifier, None)
                    await self.send(
                        frame({"type": "reject_subscription", "identifier": identifier})
                    )
                    continue
                await self.send(
                    '{"identifier":'
                    + frame(identifier)
                    + ',"message":'
                    + event.message.decode()
                    + "}"
                )
        if self.websocket.application_state != WebSocketState.DISCONNECTED:
            await self.websocket.close(code=1008)

    async def run(self) -> None:
        _clients.add(self.client)
        await self.send(frame({"type": "welcome"}))
        task = asyncio.create_task(self.pump())
        try:
            while not self.client.closed:
                try:
                    raw = await self.websocket.receive_text()
                except WebSocketDisconnect, RuntimeError, KeyError:
                    break
                if len(raw) > 65536 or not await self.handle(raw):
                    break
        finally:
            task.cancel()
            _clients.discard(self.client)
            for subscription in self.subscriptions.values():
                if subscription.channel == "PresenceChannel":
                    await db.awrite(presence, self.identity.user_id, subscription.room, "absent")

    async def handle(self, raw: str) -> bool:
        try:
            data = as_dict(json.loads(raw))
        except ValueError:
            return False
        if data is None:
            return False
        identifier = data.get("identifier", "")
        command = data.get("command")
        if not isinstance(identifier, str) or len(identifier) > 8192:
            return False
        if command == "subscribe":
            subscription = authorize(self.identity, identifier)
            if subscription is None:
                await self.send(frame({"type": "reject_subscription", "identifier": identifier}))
                return True
            if identifier not in self.subscriptions and subscription.channel == "PresenceChannel":
                await db.awrite(presence, self.identity.user_id, subscription.room, "present")
            self.subscriptions[identifier] = subscription
            await self.send(frame({"type": "confirm_subscription", "identifier": identifier}))
        elif command == "unsubscribe":
            removed = self.subscriptions.pop(identifier, None)
            if removed is not None and removed.channel == "PresenceChannel":
                await db.awrite(presence, self.identity.user_id, removed.room, "absent")
        elif command == "message" and identifier in self.subscriptions:
            await self.message(identifier, data.get("data"))
        return True

    async def message(self, identifier: str, payload: object) -> None:
        subscription = authorize(self.identity, identifier)
        if subscription is None or not isinstance(payload, str):
            return
        try:
            body = as_dict(json.loads(payload))
        except ValueError:
            return
        action = body.get("action") if body is not None else None
        if subscription.channel == "PresenceChannel" and action == "refresh":
            await db.awrite(presence, self.identity.user_id, subscription.room, "refresh")
        elif subscription.channel == "TypingNotificationsChannel" and action in ("start", "stop"):
            user = {"id": self.identity.user_id, "name": self.identity.user_name}
            await asyncio.get_running_loop().run_in_executor(
                None, publish, subscription.stream, {"action": action, "user": user}
            )
