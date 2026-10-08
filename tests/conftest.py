"""Real temporary Rails-shaped SQLite installations for every test."""

import os

os.environ.setdefault("SECRET_KEY_BASE", "tests-only")

import shutil
import sqlite3
import tempfile
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
from starlette.testclient import TestClient

from campfire import config, db, domain, jobs
from campfire.asgi import app
from campfire.models import Message, Room, User
from campfire.response_cache import cache

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "campfire/schema.sql").read_text()

type SqlValue = str | int | float | bytes | None


@dataclass
class World:
    root: Path
    database: Path
    files: Path
    admin: User
    member: User
    other: User
    room: Room
    private: Room
    client: TestClient
    token: str

    def new_client(self, base_url: str = "http://testserver") -> TestClient:
        client = TestClient(app, base_url=base_url, follow_redirects=False)
        return client

    def login(self, client: TestClient, user: User) -> str:
        response = client.get("/session/new")
        assert b'name="csrf-token"' not in response.content
        token = "legacy-tab-token"
        response = client.post(
            "/session",
            data={
                "email_address": user.email_address or "",
                "password": "secret",
                "authenticity_token": token,
            },
        )
        assert response.status_code == 302, response.text
        return token

    def post(
        self,
        path: str,
        data: dict[str, str | list[str]] | None = None,
        method: str | None = None,
        client: TestClient | None = None,
        token: str | None = None,
    ) -> httpx2.Response:
        values: dict[str, str | list[str]] = dict(data or {})
        values["authenticity_token"] = token or self.token
        if method:
            values["_method"] = method
        return (client or self.client).post(path, data=values)

    def message(
        self,
        user: User | None = None,
        room: Room | None = None,
        body: str = "Coffee <strong>is good</strong>",
    ) -> Message:
        return domain.create_message(user or self.admin, room or self.room, body)

    def sql(self, query: str, *params: SqlValue) -> list[tuple[SqlValue, ...]]:
        with closing(sqlite3.connect(self.database)) as connection:
            rows: list[tuple[SqlValue, ...]] = connection.execute(query, params).fetchall()
            connection.commit()
            return rows

    def scalar(self, query: str, *params: SqlValue) -> SqlValue:
        rows = self.sql(query, *params)
        return rows[0][0] if rows else None

    def foreign_write(self, query: str, *params: SqlValue) -> None:
        self.sql(query, *params)


def set_cookie(client: TestClient, name: str, value: str) -> None:
    """Replace a cookie the server set (httpx stores it under `testserver.local`)."""
    for cookie in list(client.cookies.jar):
        if cookie.name == name:
            client.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)
    client.cookies.set(name, value, domain="testserver.local")


def install(root: Path) -> tuple[Path, Path]:
    database = root / "db/production.sqlite3"
    database.parent.mkdir(parents=True)
    files = root / "files"
    files.mkdir()
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(SCHEMA)
    config.settings.storage_path = root
    config.settings.files_path = files
    config.settings.database_path = database
    config.settings.response_cache_bytes = 64 * 1024 * 1024
    config.settings.force_ssl = False
    config.settings.start_jobs = False
    config.settings.bcrypt_rounds = 4
    db.reset()
    cache.clear()
    jobs.reset_rates()
    return database, files


@pytest.fixture
def empty_root() -> Generator[Path]:
    (ROOT / "tmp").mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(dir=ROOT / "tmp"))
    original = config.settings.snapshot()
    try:
        install(directory)
        yield directory
    finally:
        db.reset()
        cache.clear()
        config.settings.restore(original)
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def w(empty_root: Path) -> Generator[World]:
    database = empty_root / "db/production.sqlite3"
    domain.create_account("Campfire", "invitation")
    admin = domain.create_user(
        name="Admin", email_address="admin@example.test", password="secret", role=1
    )
    member = domain.create_user(
        name="Member", email_address="member@example.test", password="secret"
    )
    other = domain.create_user(name="Other", email_address="other@example.test", password="secret")
    room = domain.create_room("Rooms::Open", admin, "All Talk")
    domain.grant_memberships(room, [admin, member, other])
    private = domain.create_room("Rooms::Closed", admin, "Private")
    domain.grant_memberships(private, [admin])
    with TestClient(app, base_url="http://testserver", follow_redirects=False) as client:
        world = World(
            root=empty_root,
            database=database,
            files=empty_root / "files",
            admin=admin,
            member=member,
            other=other,
            room=room,
            private=private,
            client=client,
            token="",
        )
        world.token = world.login(client, admin)
        cache.clear()
        yield world
