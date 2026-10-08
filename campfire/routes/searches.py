"""Full-text search with recent queries."""

import re
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from starlette.responses import Response

from .. import db, messaging, rendering, response_cache
from ..clock import format_time, now
from ..http import login, redirect
from ..models import Message
from ..presentation import message_views

router = APIRouter(include_in_schema=False)
NON_WORD = re.compile(r"[^\w]")
RECENT = 10


def normalize(query: str) -> str:
    return NON_WORD.sub(" ", query).strip()


async def render(request: Request, query: str) -> Response:
    ctx = login(request)
    rows: list[Message] = []
    if query:
        ids = messaging.search_message_ids(ctx.user, query)
        if ids:
            rows = [
                Message.load(row)
                for row in db.rows(
                    f"SELECT {Message.COLUMNS} FROM messages JOIN memberships ON "
                    "memberships.room_id=messages.room_id WHERE memberships.user_id=? AND "
                    f"messages.id IN ({db.placeholders(len(ids))}) ORDER BY messages.id",
                    (ctx.user.id, *ids),
                )
            ]
    recent = [
        str(row[0])
        for row in db.rows(
            f"SELECT query FROM searches WHERE user_id=? ORDER BY updated_at DESC LIMIT {RECENT}",
            (ctx.user.id,),
        )
    ]
    return rendering.page(
        ctx, "search", Messages=message_views(rows), Query=query, RecentSearches=recent
    )


@router.get("/searches")
async def index(request: Request) -> Response:
    ctx = login(request)
    query = normalize(ctx.query("q"))
    return await response_cache.serve(ctx, "search", lambda: render(request, query))


def remember(user_id: int, query: str) -> None:
    with db.transaction():
        stamp = format_time(now())
        existing = db.integer(
            "SELECT id FROM searches WHERE user_id=? AND query=?", (user_id, query)
        )
        if existing is None:
            db.run(
                "INSERT INTO searches(created_at,query,updated_at,user_id) VALUES (?,?,?,?)",
                (stamp, query, stamp, user_id),
            )
        else:
            db.run("UPDATE searches SET updated_at=? WHERE id=?", (stamp, existing))
        db.run(
            "DELETE FROM searches WHERE id IN (SELECT id FROM searches WHERE user_id=? "
            f"ORDER BY updated_at DESC LIMIT -1 OFFSET {RECENT})",
            (user_id,),
        )


@router.post("/searches")
async def create(request: Request) -> Response:
    ctx = login(request)
    query = normalize(ctx.params.get("q"))
    if query:
        await db.awrite(remember, ctx.user.id, query)
    return redirect("/searches?" + urlencode({"q": query}))


def forget(user_id: int) -> None:
    with db.transaction():
        db.run("DELETE FROM searches WHERE user_id=?", (user_id,))


@router.delete("/searches")
@router.delete("/searches/clear")
async def clear(request: Request) -> Response:
    ctx = login(request)
    await db.awrite(forget, ctx.user.id)
    return redirect("/searches")
