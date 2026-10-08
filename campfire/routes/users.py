"""Profiles, avatars, user pages, autocomplete, push subscriptions and bans."""

import html
import re
import zlib
from datetime import timedelta

from fastapi import APIRouter, Request
from markupsafe import Markup
from starlette.responses import Response

from .. import config, db, domain, jobs, rails, rendering
from ..assets import asset
from ..clock import format_time, now
from ..files import serve_file
from ..http import administrator, json_response, login, not_found, redirect, status
from ..media import find_variant, variant
from ..models import DIRECT, Membership, PushSubscription, Room, User
from ..presentation import room_view, user_view
from ..push import permitted_endpoint, send
from ..queries import attached_blob, has_attachment, room_for, user_by_id
from ..request_context import context_of
from ..storage import Upload, path_for, remove_attachment, replace_attachment
from ..urls import avatar
from .public import static_response

router = APIRouter(include_in_schema=False)
AVATAR_COLORS = (
    "#AF2E1B",
    "#CC6324",
    "#3B4B59",
    "#BFA07A",
    "#ED8008",
    "#ED3F1C",
    "#BF1B1B",
    "#736B1E",
    "#D07B53",
    "#736356",
    "#AD1D1D",
    "#BF7C2A",
    "#C09C6F",
    "#698F9C",
    "#7C956B",
    "#5D618F",
    "#3B3633",
    "#67695E",
)
AVATAR_SVG = (config.REFERENCE / "app/views/users/avatars/show.svg.erb").read_text()
AVATAR_CACHE = "public, max-age=1800, stale-while-revalidate=604800"
INITIAL = re.compile(r"\b\w")


def save_profile(
    user_id: int, fields: dict[str, str], password: str, avatar_upload: Upload | None
) -> None:
    with db.transaction():
        assignments = [f"{name}=?" for name in fields]
        values: list[str | int] = list(fields.values())
        if password:
            assignments.append("password_digest=?")
            values.append(domain.password_digest(password))
        assignments.append("updated_at=?")
        values.append(format_time(now()))
        db.run(f"UPDATE users SET {','.join(assignments)} WHERE id=?", (*values, user_id))
        if avatar_upload is not None:
            replace_attachment(avatar_upload, "User", user_id, "avatar")


@router.api_route("/users/me/profile", methods=["PUT", "PATCH"])
@router.api_route("/users/{user_id}/profile", methods=["PUT", "PATCH"])
async def update_profile(request: Request) -> Response:
    ctx = login(request)
    params = ctx.params
    fields = {
        name: found
        for name in ("name", "bio", "email_address")
        if (found := params.nested("user", name)) is not None
    }
    await db.awrite(
        save_profile,
        ctx.user.id,
        fields,
        params.value("user", "password"),
        params.file("user[avatar]"),
    )
    return redirect("/users/me/profile")


@router.get("/users/me/profile")
@router.get("/users/{user_id}/profile")
async def profile(request: Request) -> Response:
    ctx = login(request)
    user = ctx.user
    rows = db.rows(
        f"SELECT {Membership.COLUMNS},{Room.COLUMNS} FROM memberships JOIN rooms ON "
        "rooms.id=memberships.room_id WHERE memberships.user_id=? ORDER BY lower(rooms.name)",
        (user.id,),
    )
    shared: list[dict[str, object]] = []
    direct: list[dict[str, object]] = []
    for row in rows:
        member, room = Membership.load(row), Room.load(row, 7)
        view = room_view(room, user)
        view.Involvement = member.involvement
        entry: dict[str, object] = {
            "Room": view,
            "Involvement": member.involvement,
            "ID": member.id,
        }
        (direct if room.type == DIRECT else shared).append(entry)
    transfer = rails.signed_id("User", user.id, "transfer", now() + timedelta(hours=4))
    return rendering.page(
        ctx,
        "profile",
        DirectMemberships=direct,
        SharedMemberships=shared,
        Memberships=direct + shared,
        AvatarAttached=has_attachment("User", user.id, "avatar"),
        Transfer=f"{ctx.origin}/session/transfers/{transfer}",
        Platform={"Chrome": True, "Firefox": False, "IOS": False, "Android": False},
        CanAdminister=user.role == 1,
    )


@router.get("/users/{user_id:int}")
async def show(request: Request, user_id: int) -> Response:
    ctx = login(request)
    user = user_by_id(user_id)
    if user is None:
        not_found()
    return rendering.page(
        ctx,
        "user",
        Subject=user_view(user),
        CanAdminister=ctx.user.role == 1,
        AvatarURL=avatar(user.id, user.updated_at),
    )


def initials_avatar(user: User) -> Response:
    initials = "".join(INITIAL.findall(user.name))
    color = AVATAR_COLORS[zlib.crc32(str(user.id).encode()) % len(AVATAR_COLORS)]
    length = 'textLength="85%" lengthAdjust="spacingAndGlyphs"' if len(initials) >= 3 else ""
    svg = (
        AVATAR_SVG.replace("<%= avatar_background_color(@user) %>", color)
        .replace("<%= @user.initials %>", html.escape(initials))
        .replace(
            '<%=raw \'textLength="85%" lengthAdjust="spacingAndGlyphs"\' if @user.initials.size >= 3 %>',
            length,
        )
    )
    return Response(svg, media_type="image/svg+xml", headers={"cache-control": AVATAR_CACHE})


@router.get("/users/{signed_id}/avatar")
async def show_avatar(request: Request, signed_id: str) -> Response:
    ctx = context_of(request)
    try:
        user = user_by_id(rails.verify_id("User", signed_id, "avatar"))
    except ValueError:
        user = None
    if user is None:
        not_found()
    blob = attached_blob("User", user.id, "avatar")
    if blob is not None:
        size = (512, 512)
        processed = find_variant(blob, size, "webp") or await db.awrite(variant, blob, size, "webp")
        return serve_file(
            ctx,
            path_for(processed.key),
            "image/webp",
            processed.filename,
            extra={"cache-control": AVATAR_CACHE},
        )
    if user.role == 2:
        return static_response(ctx, asset("default-bot-avatar.svg").lstrip("/"))
    return initials_avatar(user)


@router.delete("/users/{signed_id}/avatar")
async def remove_avatar(request: Request, signed_id: str) -> Response:
    ctx = context_of(request)
    if ctx.current_user is None:
        return status(401)
    await db.awrite(remove_attachment, "User", ctx.current_user.id, "avatar")
    return redirect("/users/me/profile")


@router.get("/autocompletable/users")
async def autocomplete(request: Request) -> Response:
    ctx = login(request)
    clauses = ["users.status=0"]
    params: list[int | str] = []
    room_id = ctx.query("room_id")
    if room_id:
        room = room_for(ctx.user.id, int(room_id)) if room_id.isdigit() else None
        if room is None:
            not_found()
        clauses.append("users.id IN (SELECT user_id FROM memberships WHERE room_id=?)")
        params.append(room.id)
    query = ctx.query("filter") or ctx.query("query")
    if query:
        clauses.append("users.name LIKE ? ESCAPE '\\'")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    users = [
        User.load(row)
        for row in db.rows(
            f"SELECT {User.COLUMNS} FROM users WHERE {' AND '.join(clauses)} "
            "ORDER BY lower(users.name) LIMIT 20",
            params,
        )
    ]
    if ctx.wants_json():
        return json_response(
            [
                {
                    "id": u.id,
                    "name": html.escape(u.name),
                    "label": u.name,
                    "avatar": avatar(u.id, u.updated_at),
                    "avatar_url": avatar(u.id, u.updated_at),
                    "sgid": rails.sgid("User", u.id),
                    "value": u.id,
                }
                for u in users
            ]
        )
    fragments = [
        rendering.render(
            "prompt-item",
            {
                "Mention": {
                    "Name": u.name,
                    "Title": u.title,
                    "SGID": rails.sgid("User", u.id),
                    "Path": f"/users/{u.id}",
                    "Avatar": avatar(u.id, u.updated_at),
                },
                "HTML": Markup(
                    f'<span class="mention" data-user-id="{u.id}">{html.escape(u.name)}</span>'
                ),
            },
        )
        for u in users
    ]
    return rendering.fragment_response("".join(fragments))


def subscriptions_for(user_id: int) -> list[PushSubscription]:
    return [
        PushSubscription.load(row)
        for row in db.rows(
            f"SELECT {PushSubscription.COLUMNS} FROM push_subscriptions WHERE user_id=? ORDER BY id",
            (user_id,),
        )
    ]


@router.get("/users/me/push_subscriptions")
async def push_subscriptions(request: Request) -> Response:
    ctx = login(request)
    return rendering.page(
        ctx,
        "push-subscriptions",
        Subscriptions=[
            {
                "ID": p.id,
                "Endpoint": p.endpoint,
                "UserAgent": p.user_agent or "",
                "UpdatedAt": p.updated_at,
            }
            for p in subscriptions_for(ctx.user.id)
        ],
    )


def save_subscription(user_id: int, endpoint: str, p256dh: str, auth: str, agent: str) -> None:
    with db.transaction():
        stamp = format_time(now())
        existing = db.integer(
            "SELECT id FROM push_subscriptions WHERE user_id=? AND endpoint=? AND p256dh_key=? "
            "AND auth_key=?",
            (user_id, endpoint, p256dh, auth),
        )
        if existing is None:
            db.run(
                "INSERT INTO push_subscriptions(auth_key,created_at,endpoint,p256dh_key,updated_at,"
                "user_agent,user_id) VALUES (?,?,?,?,?,?,?)",
                (auth, stamp, endpoint, p256dh, stamp, agent, user_id),
            )
        else:
            db.run(
                "UPDATE push_subscriptions SET user_agent=?,updated_at=? WHERE id=?",
                (agent, stamp, existing),
            )


@router.post("/users/me/push_subscriptions")
async def subscribe(request: Request) -> Response:
    ctx = login(request)
    params = ctx.params
    endpoint = params.value("push_subscription", "endpoint")
    if not permitted_endpoint(endpoint):
        return status(422)
    await db.awrite(
        save_subscription,
        ctx.user.id,
        endpoint,
        params.value("push_subscription", "p256dh_key"),
        params.value("push_subscription", "auth_key"),
        ctx.header("user-agent"),
    )
    return Response(status_code=200)


def delete_subscription(user_id: int, subscription_id: int) -> None:
    with db.transaction():
        db.run(
            "DELETE FROM push_subscriptions WHERE id=? AND user_id=?", (subscription_id, user_id)
        )


@router.delete("/users/me/push_subscriptions/{subscription_id:int}")
async def unsubscribe(request: Request, subscription_id: int) -> Response:
    ctx = login(request)
    await db.awrite(delete_subscription, ctx.user.id, subscription_id)
    return redirect("/users/me/push_subscriptions")


@router.post("/users/me/push_subscriptions/{subscription_id:int}/test_notifications")
async def test_notification(request: Request, subscription_id: int) -> Response:
    import uuid

    ctx = login(request)
    found = next((p for p in subscriptions_for(ctx.user.id) if p.id == subscription_id), None)
    if found is None:
        not_found()
    await db.awrite(
        send,
        found.endpoint or "",
        found.p256dh_key or "",
        found.auth_key or "",
        "Campfire Test",
        str(uuid.uuid4()),
        "/users/me/push_subscriptions",
        0,
    )
    return redirect("/users/me/push_subscriptions")


def ban_user(user_id: int) -> None:
    with db.transaction() as tx:
        stamp = format_time(now())
        addresses = {
            str(row[0])
            for row in db.rows(
                "SELECT DISTINCT ip_address FROM sessions WHERE user_id=? AND ip_address IS NOT "
                "NULL AND ip_address<>''",
                (user_id,),
            )
        }
        for address in sorted(addresses):
            db.run(
                "INSERT INTO bans(created_at,ip_address,updated_at,user_id) VALUES (?,?,?,?)",
                (stamp, address, stamp, user_id),
            )
        db.run("DELETE FROM sessions WHERE user_id=?", (user_id,))
        db.run("UPDATE users SET status=2,updated_at=? WHERE id=?", (stamp, user_id))
        tx.on_commit(lambda: jobs.enqueue("ban-content", {"user_id": user_id}))


def unban_user(user_id: int) -> None:
    with db.transaction():
        db.run("DELETE FROM bans WHERE user_id=?", (user_id,))
        db.run("UPDATE users SET status=0,updated_at=? WHERE id=?", (format_time(now()), user_id))


@router.post("/users/{user_id:int}/ban")
async def ban(request: Request, user_id: int) -> Response:
    administrator(request)
    if user_by_id(user_id) is None:
        not_found()
    await db.awrite(ban_user, user_id)
    return redirect(f"/users/{user_id}")


@router.delete("/users/{user_id:int}/ban")
async def unban(request: Request, user_id: int) -> Response:
    administrator(request)
    if user_by_id(user_id) is None:
        not_found()
    await db.awrite(unban_user, user_id)
    return redirect(f"/users/{user_id}")
