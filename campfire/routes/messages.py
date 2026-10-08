"""Messages for browsers and bots: paging, permalinks, creation, edits, deletion."""

import sqlite3

from fastapi import APIRouter, Request
from starlette.responses import Response

from .. import db, messaging, rendering, response_cache
from ..clock import format_time, iso_millis
from ..http import halt, json_response, not_found, redirect, status
from ..models import ROLES, Message, Room, User
from ..presentation import message_views, room_view
from ..queries import bot_user, get_message, rich_text_body, room_for, user_by_id
from ..request_context import RequestContext, context_of
from ..richtext import plain_text
from ..urls import avatar

router = APIRouter(include_in_schema=False)
PAGE = 40


def serialize(ctx: RequestContext, message: Message) -> dict[str, object]:
    body = rich_text_body(message.id)
    creator = user_by_id(message.creator_id)
    origin = ctx.origin
    return {
        "id": message.id,
        "created_at": iso_millis(message.created_at),
        "body": {
            "plain_text": plain_text(body) if body else "",
            "html": '<div class="lexxy-content">' + (body or "") + "</div>",
        },
        "creator": {
            "id": message.creator_id,
            "name": creator.name if creator else "",
            "role": ROLES[creator.role] if creator and 0 <= creator.role < 3 else "member",
            "avatar_url": origin
            + (avatar(message.creator_id, creator.updated_at) if creator else ""),
        },
        "room": {"id": message.room_id},
        "url": f"{origin}/rooms/{message.room_id}/messages/{message.id}",
    }


class Target:
    """The acting user, room and optional message of one messages request."""

    def __init__(
        self, ctx: RequestContext, user: User, room: Room, message: Message | None, bot_key: str
    ) -> None:
        self.ctx = ctx
        self.user = user
        self.room = room
        self.message = message
        self.bot_key = bot_key

    @property
    def wants_json(self) -> bool:
        return bool(self.bot_key) or self.ctx.path.endswith(".json") or self.ctx.wants_json()

    def require_message(self) -> Message:
        assert self.message is not None
        return self.message


def resolve(
    request: Request, room_id: int | None, message_id: int | None, bot_key: str = ""
) -> Target:
    ctx = context_of(request)
    if bot_key:
        bot = bot_user(bot_key)
        if bot is None:
            halt(status(401))
        user = bot
    else:
        if ctx.current_user is None:
            halt(redirect("/session/new"))
        if ctx.authenticated_by_bot:
            halt(status(403))
        user = ctx.current_user
    if room_id is None and message_id is not None:
        found = get_message(message_id)
        room_id = found.room_id if found is not None else None
    room = room_for(user.id, room_id) if room_id is not None else None
    if room is None:
        halt(status(404) if bot_key else redirect("/"))
    message = None
    if message_id is not None:
        row = db.one(
            f"SELECT {Message.COLUMNS} FROM messages WHERE room_id=? AND id=?",
            (room.id, message_id),
        )
        if row is None:
            not_found()
        message = Message.load(row)
    return Target(ctx, user, room, message, bot_key)


def page_rows(target: Target) -> list[Message]:
    ctx = target.ctx
    clauses = ["room_id=?"]
    params: list[int | str] = [target.room.id]
    for direction, operator in (("before", "<"), ("after", ">")):
        anchor = ctx.query(direction)
        if anchor:
            pivot = (
                db.value(
                    "SELECT created_at FROM messages WHERE room_id=? AND id=?",
                    (target.room.id, int(anchor)),
                )
                if anchor.isdigit()
                else None
            )
            if pivot is None:
                not_found()
            clauses.append(f"created_at{operator}?")
            params.append(str(pivot))
    ascending = bool(ctx.query("after"))
    rows = [
        Message.load(row)
        for row in db.rows(
            f"SELECT {Message.COLUMNS} FROM messages WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at{'' if ascending else ' DESC'} LIMIT {PAGE}",
            params,
        )
    ]
    if not ascending:
        rows.reverse()
    return rows


def bot_listing_headers(target: Target, rows: list[Message]) -> dict[str, str]:
    room = target.room
    total = db.value("SELECT count(*) FROM messages WHERE room_id=?", (room.id,))
    headers = {"x-total-count": str(total)}
    direction = "after" if target.ctx.query("after") else "before"
    anchor = rows[-1] if direction == "after" else rows[0]
    operator = ">" if direction == "after" else "<"
    if db.exists(
        f"SELECT 1 FROM messages WHERE room_id=? AND created_at{operator}?",
        (room.id, format_time(anchor.created_at)),
    ):
        headers["link"] = (
            f"<{target.ctx.origin}/rooms/{room.id}/{target.bot_key}/messages?"
            f'{direction}={anchor.id}>; rel="next"'
        )
    return headers


async def read(target: Target) -> Response:
    ctx = target.ctx
    rows = [target.message] if target.message is not None else page_rows(target)
    if not rows:
        return Response(status_code=204)
    if target.wants_json:
        if target.message is not None:
            return json_response(serialize(ctx, target.message))
        headers = bot_listing_headers(target, rows) if target.bot_key else None
        return json_response([serialize(ctx, row) for row in rows], headers=headers)
    if target.message is not None:
        return rendering.page(ctx, "show-message", Messages=message_views(rows))
    return rendering.fragment_response(
        rendering.render("messages", {"Messages": message_views(rows)})
    )


async def index(request: Request, room_id: int, bot_key: str = "") -> Response:
    target = resolve(request, room_id, None, bot_key)
    if bot_key:
        return await read(target)
    return await response_cache.serve(target.ctx, "messages", lambda: read(target))


@router.get("/rooms/{room_id:int}/messages")
async def messages_index(request: Request, room_id: int) -> Response:
    return await index(request, room_id)


@router.get("/rooms/{room_id:int}/{bot_key}/messages")
async def bot_index(request: Request, room_id: int, bot_key: str) -> Response:
    return await index(request, room_id, bot_key)


@router.get("/rooms/{room_id:int}/messages/{message_id:int}")
@router.get("/rooms/{room_id:int}/{bot_key}/messages/{message_id:int}")
async def show(request: Request, room_id: int, message_id: int, bot_key: str = "") -> Response:
    return await read(resolve(request, room_id, message_id, bot_key))


@router.get("/messages/{message_id:int}")
async def show_message(request: Request, message_id: int) -> Response:
    return await read(resolve(request, None, message_id))


async def edit(request: Request, room_id: int | None, message_id: int) -> Response:
    target = resolve(request, room_id, message_id)
    message = target.require_message()
    if not target.user.can_administer(message.creator_id):
        return status(403)
    return rendering.page(
        target.ctx,
        "edit-message",
        Messages=message_views([message]),
        Room=room_view(target.room, target.user),
        Body=rich_text_body(message.id) or "",
    )


@router.get("/rooms/{room_id:int}/messages/{message_id:int}/edit")
async def edit_in_room(request: Request, room_id: int, message_id: int) -> Response:
    return await edit(request, room_id, message_id)


@router.get("/messages/{message_id:int}/edit")
async def edit_message(request: Request, message_id: int) -> Response:
    return await edit(request, None, message_id)


def attachment_param(ctx: RequestContext) -> messaging.AttachmentInput | None:
    params = ctx.params
    for name in ("message[attachment]", "attachment"):
        upload = params.file(name)
        if upload is not None:
            return upload
    found = params.nested("message", "attachment")
    if found is not None:
        return found
    return params.get("attachment") if params.has("attachment") else None


async def create(request: Request, room_id: int, bot_key: str = "") -> Response:
    target = resolve(request, room_id, None, bot_key)
    ctx = target.ctx
    params = ctx.params
    multipart = ctx.header("content-type").lower().startswith("multipart/form-data")
    if bot_key and not multipart:
        body = ctx.body.decode(errors="replace")
    else:
        body = params.value("message", "body")
    if bot_key and params.file("attachment") is not None:
        body = ""
    attachment = attachment_param(ctx)
    if bot_key and not body and not attachment:
        return status(422)
    try:
        message = await db.awrite(
            messaging.create_message,
            target.user,
            target.room,
            body,
            params.value("message", "client_message_id") or None,
            attachment or None,
        )
    except ValueError, PermissionError, LookupError:
        return status(422)
    if bot_key:
        location = f"{ctx.origin}/rooms/{target.room.id}/messages/{message.id}"
        return Response(status_code=201, headers={"location": location})
    if target.wants_json:
        return json_response(serialize(ctx, message), 201)
    fragment = message.fragment
    if fragment is None:
        fragment = rendering.render("message", message_views([message])[0])
    return rendering.turbo_response(
        rendering.turbo_stream("append", messaging.room_target(target.room), fragment)
    )


@router.post("/rooms/{room_id:int}/messages")
async def create_in_room(request: Request, room_id: int) -> Response:
    return await create(request, room_id)


@router.post("/rooms/{room_id:int}/{bot_key}/messages")
async def bot_create(request: Request, room_id: int, bot_key: str) -> Response:
    return await create(request, room_id, bot_key)


async def change(
    request: Request, room_id: int | None, message_id: int, bot_key: str = ""
) -> Response:
    target = resolve(request, room_id, message_id, bot_key)
    message = target.require_message()
    ctx = target.ctx
    if not target.user.can_administer(message.creator_id):
        return status(403)
    if ctx.method == "DELETE":
        await db.awrite(messaging.delete_message, message)
        if target.wants_json:
            return Response(status_code=204)
        return rendering.turbo_response(
            rendering.turbo_stream("remove", "message_" + message.client_message_id)
        )
    attachment = attachment_param(ctx)
    if bot_key:
        body = None if attachment is not None else ctx.body.decode(errors="replace")
    else:
        body = ctx.params.nested("message", "body")
    try:
        await db.awrite(messaging.update_message, message, body, attachment)
    except ValueError, sqlite3.IntegrityError:
        return status(422)
    if target.wants_json:
        return json_response(serialize(ctx, message))
    return redirect(f"/rooms/{target.room.id}/messages/{message.id}")


@router.api_route(
    "/rooms/{room_id:int}/messages/{message_id:int}", methods=["PUT", "PATCH", "DELETE"]
)
@router.api_route(
    "/rooms/{room_id:int}/{bot_key}/messages/{message_id:int}", methods=["PUT", "PATCH", "DELETE"]
)
async def change_in_room(
    request: Request, room_id: int, message_id: int, bot_key: str = ""
) -> Response:
    return await change(request, room_id, message_id, bot_key)


@router.api_route("/messages/{message_id:int}", methods=["PUT", "PATCH", "DELETE"])
async def change_message(request: Request, message_id: int) -> Response:
    return await change(request, None, message_id)
