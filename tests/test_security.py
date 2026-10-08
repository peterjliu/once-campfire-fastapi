"""Authorization, sessions, Fetch Metadata and bot boundaries."""

from unittest.mock import patch

from starlette.testclient import TestClient

from campfire import domain, rails
from campfire.asgi import app
from campfire.middleware import request_allowed
from tests.conftest import World, set_cookie


def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def test_transfer_landing_page_loads_automatic_submit_without_get_side_effects(w: World) -> None:
    token = rails.signed_id("User", w.member.id, "transfer")
    before = w.scalar("SELECT count(*) FROM sessions")
    with client() as anonymous:
        response = anonymous.get("/session/transfers/" + token)
    assert response.status_code == 200
    assert b'data-controller="auto-submit"' in response.content
    assert b'<script type="importmap"' in response.content
    assert b'name="_method" value="put"' in response.content
    assert b"</form>" in response.content
    assert w.scalar("SELECT count(*) FROM sessions") == before


def test_transfer_put_signs_in_and_rejects_tampering(w: World) -> None:
    token = rails.signed_id("User", w.member.id, "transfer")
    with client() as anonymous:
        assert anonymous.put("/session/transfers/" + token + "x").status_code == 400
        response = anonymous.post("/session/transfers/" + token, data={"_method": "put"})
        assert response.status_code == 302
        assert "session_token" in response.cookies


def test_existing_session_id_reuses_cookie_without_unused_random_generation(w: World) -> None:
    cookie = w.client.cookies["_campfire_session"]
    original = rails.decrypt_cookie("_campfire_session", cookie)
    assert isinstance(original, dict)
    assert "session_id" in original
    with patch("campfire.middleware.secrets.token_hex") as random:
        response = w.client.get("/up")
    random.assert_not_called()
    assert response.status_code == 200
    assert "_campfire_session" not in response.cookies
    assert w.client.cookies["_campfire_session"] == cookie
    assert rails.decrypt_cookie("_campfire_session", cookie) == original


def test_missing_session_id_is_generated_once_and_preserves_old_cookie_fields(w: World) -> None:
    with client() as fresh:
        set_cookie(
            fresh,
            "_campfire_session",
            rails.encrypt_cookie("_campfire_session", {"return_to_after_authenticating": "/users"}),
        )
        with patch("campfire.middleware.secrets.token_hex", return_value="a" * 32) as random:
            response = fresh.get("/up")
            cookie = fresh.cookies["_campfire_session"]
            again = fresh.get("/up")
        random.assert_called_once_with(16)
        assert response.status_code == 200
        assert again.status_code == 200
        assert "_campfire_session" in response.cookies
        assert "_campfire_session" not in again.cookies
        assert rails.decrypt_cookie("_campfire_session", cookie) == {
            "session_id": "a" * 32,
            "return_to_after_authenticating": "/users",
        }
        assert fresh.cookies["_campfire_session"] == cookie


def test_membership_required_even_for_open_rooms(w: World) -> None:
    w.foreign_write("DELETE FROM memberships WHERE room_id=? AND user_id=?", w.room.id, w.member.id)
    with client() as other:
        token = w.login(other, w.member)
        assert other.get(f"/rooms/{w.room.id}").status_code == 302
        response = w.post(
            f"/rooms/{w.room.id}/messages",
            {"message[body]": "intrusion"},
            client=other,
            token=token,
        )
        assert response.status_code == 302
    assert w.scalar("SELECT count(*) FROM messages") == 0


def test_search_and_sidebar_do_not_leak_closed_rooms(w: World) -> None:
    w.message(room=w.private, body="coffee confidential")
    with client() as other:
        w.login(other, w.member)
        assert b"confidential" not in other.get("/searches?q=coffee").content
        assert b"Private" not in other.get("/users/me/sidebar").content


def test_other_member_cannot_edit_message(w: World) -> None:
    message = w.message()
    with client() as other:
        token = w.login(other, w.member)
        edit = f"/rooms/{w.room.id}/messages/{message.id}/edit"
        assert other.get(edit).status_code == 403
        response = w.post(
            f"/rooms/{w.room.id}/messages/{message.id}",
            {"message[body]": "hacked"},
            "patch",
            other,
            token,
        )
        assert response.status_code == 403


def test_direct_namespace_cannot_widen_room_privacy(w: World) -> None:
    direct = domain.create_room("Rooms::Direct", w.admin)
    domain.grant_memberships(direct, [w.admin, w.member])
    assert w.client.get(f"/rooms/opens/{direct.id}/edit").status_code == 404
    response = w.post(f"/rooms/opens/{direct.id}", {"room[name]": "exposed"}, "patch")
    assert response.status_code == 404
    assert w.scalar("SELECT type FROM rooms WHERE id=?", direct.id) == "Rooms::Direct"


def test_boost_ownership_and_content_limit(w: World) -> None:
    message = w.message()
    assert w.post(f"/messages/{message.id}/boosts", {"boost[content]": "👍"}).status_code == 302
    boost_id = w.scalar("SELECT id FROM boosts WHERE message_id=?", message.id)
    with client() as other:
        token = w.login(other, w.member)
        response = w.post(
            f"/messages/{message.id}/boosts/{boost_id}", method="delete", client=other, token=token
        )
        assert response.status_code == 404
    assert w.post(f"/messages/{message.id}/boosts", {"boost[content]": "x" * 17}).status_code == 422
    response = w.post(f"/messages/{message.id}/boosts/{boost_id}", method="delete")
    assert response.status_code == 200


def test_fetch_metadata_and_cross_origin_fail_closed(w: World) -> None:
    path = f"/rooms/{w.room.id}/messages"
    for site in ("cross-site", "none", "invalid", ""):
        response = w.client.post(
            path, data={"message[body]": "hacked"}, headers={"sec-fetch-site": site}
        )
        assert response.status_code == 422, site
    response = w.client.post(
        path,
        data={"message[body]": "hacked"},
        headers={"sec-fetch-site": "same-site", "origin": "https://attacker.test"},
    )
    assert response.status_code == 422
    assert w.scalar("SELECT count(*) FROM messages") == 0


def test_fetch_metadata_policy_matrix() -> None:
    failures: list[tuple[str, bool, bool, str | None, str | None]] = []
    for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
        for secure in (False, True):
            scheme = "https" if secure else "http"
            base = scheme + "://testserver"
            for forced in (False, True):
                for site in (
                    None,
                    "same-origin",
                    "same-site",
                    "cross-site",
                    "none",
                    "invalid",
                    "",
                    "Same-Origin",
                    "same-origin, same-site",
                ):
                    for origin in (None, base, "null", "https://attacker.test"):
                        expected = method in ("GET", "HEAD") or (
                            (origin is None or origin == base)
                            and (
                                site in ("same-origin", "same-site")
                                or (site is None and not secure and not forced)
                            )
                        )
                        actual = request_allowed(method, scheme, "testserver", origin, site, forced)
                        if actual != expected:
                            failures.append((method, secure, forced, site, origin))
    assert failures == []


def test_no_token_secure_login_and_legacy_cookie(w: World) -> None:
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as secure:
        set_cookie(
            secure,
            "_campfire_session",
            rails.encrypt_cookie(
                "_campfire_session", {"session_id": "old-tab", "_csrf_token": "legacy-value"}
            ),
        )
        data = {"email_address": w.admin.email_address or "", "password": "secret"}
        assert secure.post("/session", data=data).status_code == 422
        response = secure.post("/session", data=data, headers={"sec-fetch-site": "same-origin"})
        assert response.status_code == 302
        page = secure.get(f"/rooms/{w.room.id}")
        assert page.status_code == 200
        assert b'name="authenticity_token"' not in page.content
        assert b'name="csrf-token"' not in page.content


def test_login_rate_limit_and_invalid_password(w: World) -> None:
    with client() as anonymous:
        data = {"email_address": "admin@example.test", "password": "wrong"}
        statuses = [anonymous.post("/session", data=data).status_code for _ in range(11)]
    # The fixture login already counted once against the ten-per-three-minutes limit.
    assert statuses == [401] * 9 + [429] * 2


def test_bot_cannot_enter_browser_routes_and_real_raw_api(w: World) -> None:
    bot = domain.create_user(name="Bot", role=2, bot_token="abcdefgh1234")
    domain.grant_memberships(w.room, [bot])
    key = f"{bot.id}-{bot.bot_token}"
    with client() as anonymous:
        assert anonymous.get(f"/rooms/{w.room.id}?bot_key={key}").status_code == 403
        response = anonymous.post(
            f"/rooms/{w.room.id}/{key}/messages",
            content=b"plain webhook reply",
            headers={"content-type": "text/plain"},
        )
        assert response.status_code == 201
        assert "/messages/" in response.headers["location"]
        message_id = w.scalar("SELECT id FROM messages WHERE creator_id=?", bot.id)
        body = w.scalar(
            "SELECT body FROM action_text_rich_texts WHERE record_id=? AND record_type='Message'",
            message_id,
        )
        assert body == "plain webhook reply"
        blocked = anonymous.post(
            f"/rooms/{w.private.id}/{key}/messages",
            content=b"blocked",
            headers={"content-type": "text/plain"},
        )
        assert blocked.status_code == 404
        wrong = anonymous.post(
            f"/rooms/{w.room.id}/wrong-key/messages",
            content=b"no-auth",
            headers={"content-type": "text/plain"},
        )
        assert wrong.status_code == 401
        listing = anonymous.get(f"/rooms/{w.room.id}/{key}/messages")
        assert listing.status_code == 200
        assert listing.headers["x-total-count"] == "1"
        assert listing.json()[0]["body"]["plain_text"] == "plain webhook reply"


def test_admin_account_rows_unique_and_direct_room_all_members_can_delete(w: World) -> None:
    response = w.client.get("/account/edit")
    assert response.content.count(f'id="role_user_{w.admin.id}"'.encode()) == 1
    direct = domain.create_room("Rooms::Direct", w.admin)
    domain.grant_memberships(direct, [w.admin, w.member])
    with client() as other:
        token = w.login(other, w.member)
        response = w.post(f"/rooms/directs/{direct.id}", method="delete", client=other, token=token)
        assert response.status_code == 302
    assert w.scalar("SELECT count(*) FROM rooms WHERE id=?", direct.id) == 0


def test_banned_address_is_rejected(w: World) -> None:
    w.foreign_write(
        "INSERT INTO bans(created_at,ip_address,updated_at,user_id) VALUES "
        "('2026-01-01 00:00:00','testclient','2026-01-01 00:00:00',?)",
        w.other.id,
    )
    assert w.client.get("/up").status_code == 403


def test_security_headers_and_unknown_paths(w: World) -> None:
    response = w.client.get("/up")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert w.client.get("/no/such/page").status_code == 404


def test_turbo_stream_attributes_are_escaped() -> None:
    from campfire import rendering

    assert rendering.turbo_stream("remove", 'message_x"><img src=x>') == (
        '<turbo-stream action="remove" target="message_x&quot;&gt;&lt;img src=x&gt;">'
        "</turbo-stream>"
    )
    assert rendering.turbo_stream("append", "t", "<b>f</b>", maintain_scroll=True) == (
        '<turbo-stream action="append" target="t" maintain_scroll="true">'
        "<template><b>f</b></template></turbo-stream>"
    )


def test_hostile_client_message_id_cannot_break_out_of_turbo_stream(w: World) -> None:
    hostile = 'x"><img src=x onerror=alert(1)>'
    response = w.post(
        f"/rooms/{w.room.id}/messages",
        {"message[body]": "hi", "message[client_message_id]": hostile},
    )
    assert response.status_code == 200
    message_id = w.scalar("SELECT id FROM messages WHERE client_message_id=?", hostile)
    assert isinstance(message_id, int)
    removal = w.post(f"/rooms/{w.room.id}/messages/{message_id}", method="delete")
    assert removal.text == (
        '<turbo-stream action="remove" '
        'target="message_x&quot;&gt;&lt;img src=x onerror=alert(1)&gt;"></turbo-stream>'
    )
