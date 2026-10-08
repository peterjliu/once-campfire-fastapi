"""Boosts from browsers and bots."""

from fastapi import APIRouter, Request
from starlette.responses import Response

from .. import db, messaging, rendering
from ..clock import iso_millis
from ..http import json_response, login, not_found, redirect, status
from ..models import Message, User
from ..presentation import message_views
from ..queries import bot_user
from ..request_context import context_of
from ..urls import avatar

router = APIRouter(include_in_schema=False)
MAX_CONTENT = 16


def member_message(user: User, message_id: int, room_id: int | None = None) -> Message:
    query = (
        f"SELECT {Message.COLUMNS} FROM messages JOIN memberships ON "
        "memberships.room_id=messages.room_id WHERE messages.id=? AND memberships.user_id=?"
    )
    params: list[int] = [message_id, user.id]
    if room_id is not None:
        query += " AND messages.room_id=?"
        params.append(room_id)
    row = db.one(query, params)
    if row is None:
        not_found()
    return Message.load(row)


def valid_content(content: str) -> bool:
    return bool(content.strip()) and len(content) <= MAX_CONTENT


@router.get("/messages/{message_id:int}/boosts")
async def index(request: Request, message_id: int) -> Response:
    ctx = login(request)
    message = member_message(ctx.user, message_id)
    return rendering.page(ctx, "boosts-index", Messages=message_views([message]))


@router.get("/messages/{message_id:int}/boosts/new")
async def new(request: Request, message_id: int) -> Response:
    ctx = login(request)
    message = member_message(ctx.user, message_id)
    return rendering.page(ctx, "new-boost", Messages=message_views([message]))


@router.post("/messages/{message_id:int}/boosts")
async def create(request: Request, message_id: int) -> Response:
    ctx = login(request)
    message = member_message(ctx.user, message_id)
    content = ctx.params.value("boost", "content")
    if not valid_content(content):
        return status(422)
    await db.awrite(messaging.create_boost, message, ctx.user, content, scroll=True)
    return redirect(f"/messages/{message.id}/boosts")


def own_boost(message: Message, boost_id: int, booster_id: int) -> None:
    if not db.exists(
        "SELECT 1 FROM boosts WHERE id=? AND message_id=? AND booster_id=?",
        (boost_id, message.id, booster_id),
    ):
        not_found()


@router.delete("/messages/{message_id:int}/boosts/{boost_id:int}")
async def destroy(request: Request, message_id: int, boost_id: int) -> Response:
    ctx = login(request)
    message = member_message(ctx.user, message_id)
    own_boost(message, boost_id, ctx.user.id)
    await db.awrite(messaging.delete_boost, message, boost_id)
    return rendering.turbo_response(rendering.turbo_stream("remove", f"boost_{boost_id}"))


def bot(bot_key: str) -> User:
    found = bot_user(bot_key)
    if found is None:
        from ..http import halt

        halt(status(401))
    return found


@router.post("/rooms/{room_id:int}/{bot_key}/messages/{message_id:int}/boosts")
async def bot_create(request: Request, room_id: int, bot_key: str, message_id: int) -> Response:
    ctx = context_of(request)
    user = bot(bot_key)
    message = member_message(user, message_id, room_id)
    content = ctx.body.decode(errors="replace")
    if not valid_content(content):
        return status(422)
    boost_id = await db.awrite(messaging.create_boost, message, user, content, scroll=False)
    created = db.value("SELECT created_at FROM boosts WHERE id=?", (boost_id,))
    from ..clock import parse_time

    origin = ctx.origin
    return json_response(
        {
            "id": boost_id,
            "content": content,
            "created_at": iso_millis(parse_time(str(created))),
            "booster": {
                "id": user.id,
                "name": user.name,
                "role": "bot",
                "avatar_url": origin + avatar(user.id, user.updated_at),
            },
            "message": {
                "id": message.id,
                "url": f"{origin}/rooms/{room_id}/messages/{message.id}",
            },
        },
        201,
    )


@router.delete("/rooms/{room_id:int}/{bot_key}/messages/{message_id:int}/boosts/{boost_id:int}")
async def bot_destroy(
    request: Request, room_id: int, bot_key: str, message_id: int, boost_id: int
) -> Response:
    user = bot(bot_key)
    message = member_message(user, message_id, room_id)
    own_boost(message, boost_id, user.id)
    await db.awrite(messaging.delete_boost, message, boost_id, touch=False)
    return Response(status_code=204)
