"""Rooms: the chat page, sidebar, settings, involvement, refresh and room forms."""

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from starlette.responses import Response

from .. import db, domain, rails, rendering, response_cache
from ..clock import format_time, now
from ..http import login, not_found, redirect, status
from ..models import ADMINISTRATOR, DIRECT, Membership, Message, Room, User
from ..presentation import RoomView, message_views, room_view, room_views, user_view
from ..queries import account, membership, room_for
from ..rendering import involvement_order

router = APIRouter(include_in_schema=False)
PAGE = 40
KINDS = {"opens", "closeds", "directs"}


def messages_where(where: str, params: tuple[int | str, ...], order: str) -> list[Message]:
    return [
        Message.load(row)
        for row in db.rows(
            f"SELECT {Message.COLUMNS} FROM messages WHERE {where} ORDER BY {order} LIMIT {PAGE}",
            params,
        )
    ]


def room_messages(room: Room, selected_id: int | None) -> list[Message]:
    selected = None
    if selected_id is not None:
        row = db.one(
            f"SELECT {Message.COLUMNS} FROM messages WHERE room_id=? AND id=?",
            (room.id, selected_id),
        )
        selected = Message.load(row) if row is not None else None
    if selected is None:
        latest = messages_where("room_id=?", (room.id,), "created_at DESC")
        latest.reverse()
        return latest
    stamp = format_time(selected.created_at)
    before = messages_where("room_id=? AND created_at<?", (room.id, stamp), "created_at DESC")
    before.reverse()
    after = messages_where("room_id=? AND created_at>?", (room.id, stamp), "created_at")
    return [*before, selected, *after]


async def render_room(request: Request, room: Room, selected_id: int | None) -> Response:
    ctx = login(request)
    messages = room_messages(room, selected_id)
    member = membership(room.id, ctx.user.id)
    return rendering.page(
        ctx,
        "room",
        Room=room_view(room, ctx.user),
        Messages=message_views(messages, ctx.origin),
        LoadedAt=int(room.updated_at.timestamp() * 1000),
        Stream=rails.sign_stream(rails.stream_name(room.type, room.id)),
        Involvement=member.involvement if member else "mentions",
        Invitation=False,
    )


async def show(request: Request, room_id: int, selected_id: int | None) -> Response:
    ctx = login(request)
    room = room_for(ctx.user.id, room_id)
    if room is None:
        return redirect("/")
    ctx.last_room = room.id
    return await response_cache.serve(ctx, "room", lambda: render_room(request, room, selected_id))


@router.get("/rooms")
async def index(request: Request) -> Response:
    ctx = login(request)
    row = db.one(
        "SELECT rooms.id FROM rooms JOIN memberships ON memberships.room_id=rooms.id "
        "WHERE memberships.user_id=? ORDER BY rooms.id DESC LIMIT 1",
        (ctx.user.id,),
    )
    return redirect(f"/rooms/{int(row[0])}" if row is not None else "/")


@router.get("/rooms/{room_id:int}")
async def room_page(request: Request, room_id: int) -> Response:
    return await show(request, room_id, None)


@router.get("/rooms/{room_id:int}/@{message_id:int}")
async def room_at_message(request: Request, room_id: int, message_id: int) -> Response:
    return await show(request, room_id, message_id)


async def destroy(request: Request, room: Room, kind: str) -> Response:
    ctx = login(request)
    if kind != "directs" and not ctx.user.can_administer(room.creator_id):
        return status(403)
    await db.awrite(domain.delete_room, room)
    return redirect("/")


@router.delete("/rooms/{room_id:int}")
async def delete(request: Request, room_id: int) -> Response:
    ctx = login(request)
    room = room_for(ctx.user.id, room_id)
    if room is None:
        return redirect("/")
    if not ctx.user.can_administer(room.creator_id):
        return status(403)
    await db.awrite(domain.delete_room, room)
    return redirect("/")


@router.get("/users/me/sidebar")
async def sidebar(request: Request) -> Response:
    ctx = login(request)
    return await response_cache.serve(ctx, "sidebar", lambda: render_sidebar(request))


async def render_sidebar(request: Request) -> Response:
    ctx = login(request)
    rows = db.rows(
        f"SELECT {Membership.COLUMNS},{Room.COLUMNS} FROM memberships JOIN rooms "
        "ON rooms.id=memberships.room_id WHERE memberships.user_id=? AND "
        "(memberships.involvement IS NULL OR memberships.involvement<>'invisible') "
        "ORDER BY lower(rooms.name)",
        (ctx.user.id,),
    )
    pairs = [(Membership.load(row), Room.load(row, 7)) for row in rows]
    directs = sorted(
        (p for p in pairs if p[1].type == DIRECT), key=lambda p: p[1].updated_at, reverse=True
    )
    ordered = directs + [p for p in pairs if p[1].type != DIRECT]
    views = room_views([room for _, room in ordered], ctx.user)
    for view, (member, _) in zip(views, ordered, strict=True):
        view.Unread = member.unread_at is not None
    return rendering.page(ctx, "sidebar", SidebarRooms=views, Placeholders=[])


@router.get("/rooms/{room_id:int}/refresh")
async def refresh(request: Request, room_id: int) -> Response:
    ctx = login(request)
    room = room_for(ctx.user.id, room_id)
    if room is None:
        not_found()
    try:
        loaded = datetime.fromtimestamp(float(ctx.query("since", "0")) / 1000, tz=UTC)
    except ValueError, OverflowError:
        loaded = now()
    stamp = format_time(loaded)
    fresh = messages_where("room_id=? AND created_at>?", (room.id, stamp), "created_at")
    fresh_ids = {m.id for m in fresh}
    updated = [
        m
        for m in messages_where(
            "room_id=? AND updated_at>?", (room.id, stamp), "+messages.created_at DESC"
        )
        if m.id not in fresh_ids
    ]
    rows = fresh + list(reversed(updated))
    streams: list[str] = []
    for message, view in zip(rows, message_views(rows), strict=True):
        if message.created_at > loaded:
            action, target = "append", f"messages_rooms_{room.kind}_{room.id}"
        else:
            action, target = "replace", "message_" + message.client_message_id
        streams.append(rendering.turbo_stream(action, target, rendering.render("message", view)))
    return rendering.turbo_response("".join(streams))


def set_involvement(room_id: int, user_id: int, choice: str) -> None:
    with db.transaction():
        db.run(
            "UPDATE memberships SET involvement=?,updated_at=? WHERE room_id=? AND user_id=?",
            (choice, format_time(now()), room_id, user_id),
        )


async def involvement_page(request: Request, room_id: int, change: bool) -> Response:
    ctx = login(request)
    room = room_for(ctx.user.id, room_id)
    if room is None:
        not_found()
    if change:
        choice = ctx.params.get("involvement") or ctx.query("involvement")
        if choice not in involvement_order(room.type):
            return status(422)
        await db.awrite(set_involvement, room.id, ctx.user.id, choice)
    member = membership(room.id, ctx.user.id)
    return rendering.page(
        ctx,
        "involvement",
        Room=room_view(room, ctx.user),
        Involvement=member.involvement if member else "mentions",
    )


@router.get("/rooms/{room_id:int}/involvement")
@router.get("/rooms/{room_id:int}/settings")
async def involvement(request: Request, room_id: int) -> Response:
    return await involvement_page(request, room_id, False)


@router.api_route("/rooms/{room_id:int}/involvement", methods=["PUT", "PATCH"])
@router.api_route("/rooms/{room_id:int}/settings", methods=["PUT", "PATCH"])
async def update_involvement(request: Request, room_id: int) -> Response:
    return await involvement_page(request, room_id, True)


def form_room(request: Request, kind: str, room_id: int | None) -> Room | None:
    ctx = login(request)
    if kind not in KINDS:
        not_found()
    if room_id is None:
        return None
    room = room_for(ctx.user.id, room_id)
    if room is None or (room.type == DIRECT) != (kind == "directs"):
        not_found()
    return room


def creation_forbidden(user: User, kind: str) -> bool:
    if kind == "directs" or user.role == ADMINISTRATOR:
        return False
    found = account()
    if found is None or not found.settings:
        return False
    import json

    from ..jsonvalue import as_dict

    settings = as_dict(json.loads(found.settings)) or {}
    return bool(settings.get("restrict_room_creation_to_administrators"))


def render_form(request: Request, kind: str, room: Room | None) -> Response:
    ctx = login(request)
    type_name = "Rooms::" + kind[:-1].capitalize()
    display = room_view(room, ctx.user) if room is not None else RoomView(0, "New room", type_name)
    display.Type = type_name
    users = [
        User.load(row)
        for row in db.rows(f"SELECT {User.COLUMNS} FROM users WHERE status=0 ORDER BY lower(name)")
    ]
    if room is not None:
        selected = {
            int(row[0])
            for row in db.rows("SELECT user_id FROM memberships WHERE room_id=?", (room.id,))
        }
    else:
        selected = {ctx.user.id}
    users.sort(key=lambda u: u.id not in selected)
    return rendering.page(
        ctx,
        "room-form",
        Room=display,
        Users=[user_view(u) for u in users],
        Selected=dict.fromkeys(selected, True),
        UserDivider=len(selected),
        CanAdminister=room is None or ctx.user.can_administer(room.creator_id) or kind == "directs",
        BackPath=f"/rooms/{room.id}" if room is not None else "/",
    )


@router.get("/rooms/{kind}/new")
async def new_room(request: Request, kind: str) -> Response:
    ctx = login(request)
    form_room(request, kind, None)
    if creation_forbidden(ctx.user, kind):
        return status(403)
    return render_form(request, kind, None)


@router.get("/rooms/{kind}/{room_id:int}/edit")
async def edit_room(request: Request, kind: str, room_id: int) -> Response:
    return render_form(request, kind, form_room(request, kind, room_id))


@router.get("/rooms/{kind}/{room_id:int}")
async def show_room_of_kind(request: Request, kind: str, room_id: int) -> Response:
    room = form_room(request, kind, room_id)
    assert room is not None
    return redirect(f"/rooms/{room.id}")


@router.get("/rooms/{kind}")
async def room_kind_form(request: Request, kind: str) -> Response:
    ctx = login(request)
    form_room(request, kind, None)
    if creation_forbidden(ctx.user, kind):
        return status(403)
    return render_form(request, kind, None)


@router.delete("/rooms/{kind}/{room_id:int}")
async def delete_room_of_kind(request: Request, kind: str, room_id: int) -> Response:
    room = form_room(request, kind, room_id)
    assert room is not None
    return await destroy(request, room, kind)


def save(kind: str, actor: User, room: Room | None, name: str, ids: set[int]) -> Room:
    saved, existing = domain.save_room(kind, actor, room, name, ids)
    if not existing:
        domain.broadcast_room(saved, kind)
    return saved


async def submit(request: Request, kind: str, room: Room | None) -> Response:
    ctx = login(request)
    if room is None and creation_forbidden(ctx.user, kind):
        return status(403)
    if room is not None and not ctx.user.can_administer(room.creator_id):
        return status(403)
    params = ctx.params
    raw_ids = params.getlist("user_ids[]") or params.getlist("user_ids")
    ids = {int(value) for value in raw_ids if value.isdigit()}
    saved = await db.awrite(save, kind, ctx.user, room, params.value("room", "name"), ids)
    return redirect(f"/rooms/{saved.id}")


@router.post("/rooms/{kind}")
async def create_room(request: Request, kind: str) -> Response:
    form_room(request, kind, None)
    return await submit(request, kind, None)


@router.api_route("/rooms/{kind}/{room_id:int}", methods=["POST", "PUT", "PATCH"])
async def update_room(request: Request, kind: str, room_id: int) -> Response:
    return await submit(request, kind, form_room(request, kind, room_id))
