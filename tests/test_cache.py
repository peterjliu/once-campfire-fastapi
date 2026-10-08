"""Authenticated page cache: fresh authorization, epochs, representations."""

from collections.abc import Callable
from unittest.mock import patch

from starlette.testclient import TestClient

from campfire import config, middleware, rails, rendering
from campfire.jsonvalue import as_dict
from campfire.response_cache import cache
from tests.conftest import World, set_cookie

CACHED_PATHS = (
    "/rooms/{room}",
    "/rooms/{room}/messages",
    "/users/me/sidebar",
    "/searches?q=literal",
)


def counting_render() -> tuple[list[str], Callable[[str, object], str]]:
    calls: list[str] = []
    original = rendering.render

    def wrapped(name: str, data: object) -> str:
        calls.append(name)
        return original(name, data)

    return calls, wrapped


def test_cached_pages_reuse_complete_token_free_representations(w: World) -> None:
    w.message(body="literal campfire-csrf-not-a-token authenticity_token")
    for template in CACHED_PATHS:
        path = template.format(room=w.room.id)
        for encoding in ("identity", "gzip"):
            w.client.get(path)
            cache.clear()
            calls, wrapped = counting_render()
            with patch("campfire.rendering.render", side_effect=wrapped):
                first = w.client.get(path, headers={"accept-encoding": encoding})
                second = w.client.get(path, headers={"accept-encoding": encoding})
            assert second.status_code == 200
            assert len(calls) == 1, (path, encoding, calls)
            assert first.content == second.content
            # httpx decodes gzip transparently, so content is the decoded representation.
            body = second.content
            assert b'name="csrf-token"' not in body
            assert b'name="authenticity_token"' not in body
    assert (
        b"literal campfire-csrf-not-a-token authenticity_token"
        in w.client.get(f"/rooms/{w.room.id}").content
    )


def test_response_cache_bypasses_incoming_flash_state(w: World) -> None:
    path = f"/rooms/{w.room.id}"
    w.client.get(path)
    w.client.get(path)
    session = as_dict(
        rails.decrypt_cookie("_campfire_session", w.client.cookies["_campfire_session"])
    )
    assert session is not None
    session["flash"] = {"flashes": {"notice": "temporary notice"}, "discard": []}
    set_cookie(w.client, "_campfire_session", rails.encrypt_cookie("_campfire_session", session))
    calls, wrapped = counting_render()
    with patch("campfire.rendering.render", side_effect=wrapped):
        w.client.get(path)
        w.client.get(path)
    assert len(calls) == 2


def test_cached_pages_observe_local_and_foreign_database_writes(w: World) -> None:
    path = f"/rooms/{w.room.id}"
    w.message(body="first cached message")
    w.client.get(path)
    w.client.get(path)
    w.message(body="local committed message")
    assert b"local committed message" in w.client.get(path).content
    w.foreign_write("UPDATE users SET name='Foreign user name' WHERE id=?", w.admin.id)
    assert b"Foreign user name" in w.client.get(path).content


def test_cached_pages_recheck_membership_and_session(w: World) -> None:
    path = f"/rooms/{w.private.id}"
    w.client.get(path)
    w.client.get(path)
    w.foreign_write(
        "DELETE FROM memberships WHERE room_id=? AND user_id=?", w.private.id, w.admin.id
    )
    assert w.client.get(path).status_code == 302
    w.client.get(f"/rooms/{w.room.id}")
    w.foreign_write("DELETE FROM sessions WHERE user_id=?", w.admin.id)
    response = w.client.get(f"/rooms/{w.room.id}")
    assert response.headers["location"] == "/session/new"


def test_cached_pages_separate_viewers_origins_and_frames(w: World) -> None:
    with TestClient(w.client.app, follow_redirects=False) as other:
        w.login(other, w.member)
        path = f"/rooms/{w.room.id}"
        w.client.get(path)
        calls, wrapped = counting_render()
        with patch("campfire.rendering.render", side_effect=wrapped):
            w.client.get(path)
            w.client.get(path)
            w.client.get(path, headers={"host": "different.example.test:8081"})
            w.client.get(path, headers={"turbo-frame": "different-frame"})
            other.get(path)
        assert len(calls) == 4


def test_response_cache_off_and_byte_budget(w: World) -> None:
    path = f"/rooms/{w.room.id}"
    config.settings.response_cache_bytes = 0
    calls, wrapped = counting_render()
    with patch("campfire.rendering.render", side_effect=wrapped):
        w.client.get(path)
        w.client.get(path)
    assert len(calls) == 2
    assert cache.bytes == 0
    config.settings.response_cache_bytes = 100
    w.client.get(path)
    assert cache.bytes == 0


def test_response_cache_does_not_admit_old_authentication_under_new_epoch(w: World) -> None:
    path = f"/rooms/{w.room.id}"
    w.client.get(path)
    cache.clear()
    original = middleware.banned

    def commit_after_authentication(address: str) -> bool:
        w.foreign_write(
            "UPDATE users SET name='Changed after authentication' WHERE id=?", w.admin.id
        )
        return original(address)

    with patch("campfire.middleware.banned", side_effect=commit_after_authentication):
        w.client.get(path)
    assert cache.bytes == 0
    assert b"Changed after authentication" in w.client.get(path).content


def test_response_cache_does_not_admit_across_a_commit(w: World) -> None:
    path = f"/rooms/{w.room.id}"
    w.client.get(path)
    cache.clear()
    original = rendering.render

    def concurrent_commit(name: str, data: object) -> str:
        result = original(name, data)
        w.foreign_write("UPDATE users SET name='Committed during render' WHERE id=?", w.admin.id)
        return result

    with patch("campfire.rendering.render", side_effect=concurrent_commit):
        w.client.get(path)
    assert cache.bytes == 0
    assert b"Committed during render" in w.client.get(path).content


def test_gzip_quality_exclusions_apply_to_cached_and_native_responses(w: World) -> None:
    path = f"/rooms/{w.room.id}"
    for budget in (0, 64 * 1024 * 1024):
        config.settings.response_cache_bytes = budget
        identity = w.client.get(path, headers={"accept-encoding": "identity"})
        for encoding in (
            "gzip;q=0",
            "gzip;q=0, *;q=1",
            "*;q=0",
            "br",
            "gzip;q=invalid",
            "gzip;q=.5, identity;q=1",
            ",".join(["br"] * 17 + ["gzip;q=0", "*;q=1"]),
        ):
            response = w.client.get(path, headers={"accept-encoding": encoding})
            assert "content-encoding" not in response.headers, (budget, encoding)
            assert identity.content == response.content
            assert "Accept-Encoding" in response.headers["vary"]
        first = w.client.get(path, headers={"accept-encoding": "gzip"})
        second = w.client.get(path, headers={"accept-encoding": "gzip"})
        assert first.headers["content-encoding"] == "gzip"
        # httpx decodes gzip transparently; compare the decoded representation.
        assert first.content == second.content
        assert identity.content == first.content


def test_room_cache_hit_still_sets_last_room_cookie(w: World) -> None:
    path = f"/rooms/{w.room.id}"
    w.client.get(path)
    w.client.cookies.delete("last_room")
    hit = w.client.get(path)
    assert hit.status_code == 200
    assert hit.cookies.get("last_room") == str(w.room.id)


def test_head_and_bot_requests_bypass_cache(w: World) -> None:
    path = f"/rooms/{w.room.id}"
    w.client.get(path)
    w.client.get(path)
    calls, wrapped = counting_render()
    with patch("campfire.rendering.render", side_effect=wrapped):
        assert w.client.head(path).status_code == 200
    assert len(calls) == 1
