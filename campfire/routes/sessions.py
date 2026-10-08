"""Setup, sign-in, sign-out, joining and session transfers."""

import secrets
import sqlite3

from fastapi import APIRouter, Request
from starlette.responses import Response

from .. import db, domain, jobs, rails, rendering
from ..clock import format_time, now
from ..http import redirect, status
from ..models import OPEN, User
from ..queries import account, room_for, user_by_id
from ..request_context import RequestContext, context_of
from ..storage import Upload, replace_attachment

router = APIRouter(include_in_schema=False)
INVALID_LOGIN = "Too many requests or unauthorized."


def create_session(user_id: int, user_agent: str, address: str) -> str:
    token = secrets.token_urlsafe(18)
    with db.transaction():
        stamp = format_time(now())
        db.run(
            "INSERT INTO sessions(created_at,ip_address,last_active_at,token,updated_at,"
            "user_agent,user_id) VALUES (?,?,?,?,?,?,?)",
            (stamp, address, stamp, token, stamp, user_agent, user_id),
        )
    return token


async def start_session(ctx: RequestContext, user: User) -> None:
    token = await db.awrite(create_session, user.id, ctx.header("user-agent"), ctx.remote_addr)
    from ..queries import session_user

    found = session_user(token)
    if found is not None:
        ctx.current_session, ctx.current_user = found
    ctx.new_session_token = token


@router.get("/")
async def welcome(request: Request) -> Response:
    ctx = context_of(request)
    if account() is None:
        return redirect("/first_run")
    user = ctx.current_user
    if user is None:
        return redirect("/session/new")
    last = ctx.cookies.get("last_room") or str(ctx.session_data.get("last_room_id", ""))
    room = room_for(user.id, int(last)) if last.isdigit() else None
    if room is None:
        row = db.one(
            "SELECT rooms.id FROM rooms JOIN memberships ON memberships.room_id=rooms.id "
            "WHERE memberships.user_id=? ORDER BY rooms.created_at LIMIT 1",
            (user.id,),
        )
        if row is not None:
            return redirect(f"/rooms/{int(row[0])}")
        return rendering.page(ctx, "welcome")
    return redirect(f"/rooms/{room.id}")


@router.get("/session/new")
async def new_session(request: Request) -> Response:
    ctx = context_of(request)
    if not db.exists("SELECT 1 FROM users LIMIT 1"):
        return redirect("/first_run")
    return rendering.page(ctx, "login", Email=ctx.query("email_address"))


@router.post("/session")
async def create(request: Request) -> Response:
    ctx = context_of(request)
    params = ctx.params
    if not jobs.rate_limit("login:" + ctx.remote_addr, 10, 180):
        return status(429, INVALID_LOGIN)
    email = params.get("email_address")
    row = db.one(f"SELECT {User.COLUMNS} FROM users WHERE status=0 AND email_address=?", (email,))
    user = User.load(row) if row is not None else None
    if user is None or not domain.check_password(params.get("password"), user.password_digest):
        return rendering.page(ctx, "login", status=401, Error=INVALID_LOGIN, Email=email)
    await start_session(ctx, user)
    target = ctx.session_data.pop("return_to_after_authenticating", "/")
    location = target if isinstance(target, str) else "/"
    return redirect(location if location.startswith("/") and not location.startswith("//") else "/")


def end_session(session_id: int | None, user_id: int | None, endpoint: str) -> None:
    with db.transaction():
        if session_id is not None:
            db.run("DELETE FROM sessions WHERE id=?", (session_id,))
        if user_id is not None:
            db.run(
                "DELETE FROM push_subscriptions WHERE user_id=? AND endpoint=?", (user_id, endpoint)
            )


@router.delete("/session")
async def destroy(request: Request) -> Response:
    ctx = context_of(request)
    session = ctx.current_session
    user = ctx.current_user
    await db.awrite(
        end_session,
        session.id if session else None,
        user.id if user else None,
        ctx.params.get("push_subscription_endpoint"),
    )
    ctx.session_data.clear()
    ctx.clear_session_token = True
    return redirect("/")


def install(name: str, email: str, password: str, avatar: Upload | None) -> User:
    with db.transaction():
        domain.create_account("Campfire", secrets.token_urlsafe(18))
        user = domain.create_user(name=name, email_address=email, password=password, role=1)
        room = domain.create_room(OPEN, user, "All Talk")
        domain.grant_memberships(room, [user])
        if avatar is not None:
            replace_attachment(avatar, "User", user.id, "avatar")
        return user


@router.get("/first_run")
async def first_run(request: Request) -> Response:
    ctx = context_of(request)
    if account() is not None:
        return redirect("/")
    return rendering.page(ctx, "first-run")


@router.post("/first_run")
async def setup(request: Request) -> Response:
    ctx = context_of(request)
    if account() is not None:
        return redirect("/")
    params = ctx.params
    try:
        user = await db.awrite(
            install,
            params.value("user", "name"),
            params.value("user", "email_address"),
            params.value("user", "password"),
            params.file("user[avatar]"),
        )
    except sqlite3.IntegrityError:
        return redirect("/")
    await start_session(ctx, user)
    return redirect("/")


def join_account(name: str, email: str, password: str, avatar: Upload | None) -> User:
    with db.transaction():
        user = domain.create_user(name=name, email_address=email, password=password)
        if avatar is not None:
            replace_attachment(avatar, "User", user.id, "avatar")
        return user


def joinable(code: str) -> bool:
    found = account()
    return found is not None and found.join_code == code


@router.get("/join/{code}")
async def join_form(request: Request, code: str) -> Response:
    ctx = context_of(request)
    if not joinable(code):
        from ..http import not_found_response

        return not_found_response()
    if ctx.current_user is not None:
        return redirect("/")
    return rendering.page(ctx, "join", JoinCode=code)


@router.post("/join/{code}")
async def join(request: Request, code: str) -> Response:
    ctx = context_of(request)
    if not joinable(code):
        from ..http import not_found_response

        return not_found_response()
    if ctx.current_user is not None:
        return redirect("/")
    params = ctx.params
    try:
        user = await db.awrite(
            join_account,
            params.value("user", "name"),
            params.value("user", "email_address"),
            params.value("user", "password"),
            params.file("user[avatar]"),
        )
    except sqlite3.IntegrityError:
        return redirect("/session/new")
    await start_session(ctx, user)
    return redirect("/")


@router.get("/session/transfers/{transfer_id}")
async def transfer_form(request: Request, transfer_id: str) -> Response:
    return rendering.page(context_of(request), "transfer", Transfer=transfer_id)


@router.api_route("/session/transfers/{transfer_id}", methods=["PUT", "PATCH"])
async def transfer(request: Request, transfer_id: str) -> Response:
    ctx = context_of(request)
    try:
        user = user_by_id(rails.verify_id("User", transfer_id, "transfer"))
    except ValueError:
        user = None
    if user is None or user.status != 0:
        return status(400)
    await start_session(ctx, user)
    return redirect("/")
