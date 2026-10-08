"""Account settings, members, bots, custom styles and join codes."""

import json
import secrets

from fastapi import APIRouter, Request
from starlette.responses import Response

from .. import db, domain, rendering
from ..clock import format_time, now
from ..http import administrator, login, not_found, redirect, status
from ..models import ADMINISTRATOR, BOT, Room, User
from ..presentation import room_views, user_view
from ..queries import account, user_by_id
from ..storage import Upload, replace_attachment
from ..urls import avatar

router = APIRouter(include_in_schema=False)


def save_account(
    account_id: int, name: str | None, settings: dict[str, object] | None, logo: Upload | None
) -> None:
    with db.transaction():
        stamp = format_time(now())
        if name is not None:
            db.run("UPDATE accounts SET name=? WHERE id=?", (name, account_id))
        if settings is not None:
            db.run("UPDATE accounts SET settings=? WHERE id=?", (json.dumps(settings), account_id))
        db.run("UPDATE accounts SET updated_at=? WHERE id=?", (stamp, account_id))
        if logo is not None:
            replace_attachment(logo, "Account", account_id, "logo")


@router.api_route("/account", methods=["PUT", "PATCH"])
@router.api_route("/account/edit", methods=["PUT", "PATCH"])
async def update(request: Request) -> Response:
    ctx = login(request)
    if ctx.user.role != ADMINISTRATOR:
        return status(403)
    found = account()
    if found is None:
        not_found()
    params = ctx.params
    settings: dict[str, object] | None = params.nested_dict("account", "settings")
    flag = "account[settings][restrict_room_creation_to_administrators]"
    if settings is None and params.has(flag):
        settings = {
            "restrict_room_creation_to_administrators": params.get(flag) in ("1", "true", "on")
        }
    await db.awrite(
        save_account,
        found.id,
        params.nested("account", "name"),
        settings,
        params.file("account[logo]"),
    )
    return redirect("/account/edit")


@router.get("/account")
@router.get("/account/edit")
@router.get("/account/users")
async def show(request: Request) -> Response:
    ctx = login(request)
    found = account()
    if found is None:
        not_found()
    statuses = "0,2" if ctx.user.role == ADMINISTRATOR else "0"
    users = [
        User.load(row)
        for row in db.rows(
            f"SELECT {User.COLUMNS} FROM users WHERE role<>2 AND status IN ({statuses}) "
            "ORDER BY lower(name) LIMIT 500"
        )
    ]
    users.sort(key=lambda u: u.role != ADMINISTRATOR)
    members = [user_view(u) for u in users if u.role != ADMINISTRATOR]
    return rendering.page(
        ctx,
        "account",
        Users=members,
        UserDivider=0,
        CanAdminister=ctx.user.role == ADMINISTRATOR,
        JoinURL=f"{ctx.origin}/join/{found.join_code}",
        Administrators=[user_view(u) for u in users if u.role == ADMINISTRATOR],
        Members=members,
    )


def set_role(user_id: int, role: int) -> None:
    with db.transaction():
        db.run(
            "UPDATE users SET role=?,updated_at=? WHERE id=?", (role, format_time(now()), user_id)
        )


def active_user(user_id: int) -> User:
    user = user_by_id(user_id)
    if user is None or user.status != 0:
        not_found()
    return user


@router.api_route("/account/users/{user_id:int}", methods=["PUT", "PATCH"])
async def update_user(request: Request, user_id: int) -> Response:
    ctx = administrator(request)
    user = active_user(user_id)
    role = ADMINISTRATOR if ctx.params.value("user", "role") == "administrator" else 0
    await db.awrite(set_role, user.id, role)
    return redirect("/account/edit")


@router.delete("/account/users/{user_id:int}")
async def deactivate_user(request: Request, user_id: int) -> Response:
    administrator(request)
    user = active_user(user_id)
    await db.awrite(domain.deactivate, user)
    return redirect("/account/edit")


def active_bot(bot_id: int) -> User:
    row = db.one(f"SELECT {User.COLUMNS} FROM users WHERE id=? AND role=2 AND status=0", (bot_id,))
    if row is None:
        not_found()
    return User.load(row)


@router.get("/account/bots")
async def bots(request: Request) -> Response:
    ctx = administrator(request)
    entries: list[dict[str, object]] = []
    for row in db.rows(
        f"SELECT {User.COLUMNS} FROM users WHERE role=2 AND status=0 ORDER BY lower(name)"
    ):
        bot = User.load(row)
        rooms = [
            Room.load(room)
            for room in db.rows(
                f"SELECT {Room.COLUMNS} FROM rooms JOIN memberships ON "
                "memberships.room_id=rooms.id WHERE memberships.user_id=? ORDER BY rooms.id",
                (bot.id,),
            )
        ]
        entries.append({"User": user_view(bot), "Rooms": room_views(rooms, bot)})
    return rendering.page(ctx, "bots", Bots=entries)


def bot_form(request: Request, bot: User | None) -> Response:
    ctx = administrator(request)
    webhook = (
        db.value("SELECT url FROM webhooks WHERE user_id=? ORDER BY id LIMIT 1", (bot.id,))
        if bot is not None
        else None
    )
    return rendering.page(
        ctx,
        "bot-form",
        Subject=user_view(bot),
        Webhook=webhook if isinstance(webhook, str) else "",
        AvatarURL=avatar(bot.id, bot.updated_at) if bot is not None else "",
    )


@router.get("/account/bots/new")
async def new_bot(request: Request) -> Response:
    return bot_form(request, None)


@router.get("/account/bots/{bot_id:int}/edit")
async def edit_bot(request: Request, bot_id: int) -> Response:
    administrator(request)
    return bot_form(request, active_bot(bot_id))


def save_bot(bot_id: int | None, name: str, webhook_url: str, avatar_upload: Upload | None) -> None:
    with db.transaction():
        if bot_id is None:
            bot_id = domain.create_user(
                name=name, role=BOT, bot_token=domain.generate_bot_token()
            ).id
        else:
            db.run(
                "UPDATE users SET name=?,updated_at=? WHERE id=?",
                (name, format_time(now()), bot_id),
            )
        domain.set_webhook(bot_id, webhook_url)
        if avatar_upload is not None:
            replace_attachment(avatar_upload, "User", bot_id, "avatar")


async def submit_bot(request: Request, bot_id: int | None) -> Response:
    ctx = administrator(request)
    if bot_id is not None:
        active_bot(bot_id)
    params = ctx.params
    await db.awrite(
        save_bot,
        bot_id,
        params.value("user", "name"),
        params.value("user", "webhook_url"),
        params.file("user[avatar]"),
    )
    return redirect("/account/bots")


@router.post("/account/bots")
async def create_bot(request: Request) -> Response:
    return await submit_bot(request, None)


@router.api_route("/account/bots/{bot_id:int}", methods=["POST", "PUT", "PATCH"])
async def update_bot(request: Request, bot_id: int) -> Response:
    return await submit_bot(request, bot_id)


def rotate_key(bot_id: int) -> None:
    with db.transaction():
        db.run(
            "UPDATE users SET bot_token=?,updated_at=? WHERE id=?",
            (domain.generate_bot_token(), format_time(now()), bot_id),
        )


@router.api_route("/account/bots/{bot_id:int}/key", methods=["PUT", "PATCH", "POST"])
async def reset_key(request: Request, bot_id: int) -> Response:
    administrator(request)
    bot = active_bot(bot_id)
    await db.awrite(rotate_key, bot.id)
    return redirect(f"/account/bots/{bot.id}/edit")


@router.delete("/account/bots/{bot_id:int}")
async def delete_bot(request: Request, bot_id: int) -> Response:
    administrator(request)
    await db.awrite(domain.deactivate, active_bot(bot_id))
    return redirect("/account/bots")


@router.get("/account/custom_styles")
@router.get("/account/custom_styles/edit")
async def custom_styles(request: Request) -> Response:
    ctx = administrator(request)
    found = account()
    styles = found.custom_styles or "" if found is not None else ""
    return rendering.page(ctx, "custom-styles", Styles=styles, CustomStylesBody=styles)


def save_styles(account_id: int, styles: str) -> None:
    with db.transaction():
        db.run(
            "UPDATE accounts SET custom_styles=?,updated_at=? WHERE id=?",
            (styles, format_time(now()), account_id),
        )


@router.api_route("/account/custom_styles", methods=["PUT", "PATCH"])
@router.api_route("/account/custom_styles/edit", methods=["PUT", "PATCH"])
async def update_styles(request: Request) -> Response:
    ctx = administrator(request)
    found = account()
    if found is None:
        not_found()
    await db.awrite(save_styles, found.id, ctx.params.value("account", "custom_styles"))
    return redirect("/account/custom_styles/edit")


def regenerate_join_code(account_id: int) -> None:
    with db.transaction():
        db.run(
            "UPDATE accounts SET join_code=?,updated_at=? WHERE id=?",
            (secrets.token_urlsafe(18), format_time(now()), account_id),
        )


@router.post("/account/join_code")
async def join_code(request: Request) -> Response:
    administrator(request)
    found = account()
    if found is None:
        not_found()
    await db.awrite(regenerate_join_code, found.id)
    return redirect("/account/edit")
