"""Every browser page renders and its forms persist through the real routes."""

import re

from starlette.testclient import TestClient

from campfire import domain, rails
from campfire.asgi import app
from tests.conftest import World


def test_every_page_renders_complete_html(w: World) -> None:
    message = w.message()
    bot = domain.create_user(name="Helper", role=2, bot_token="helpertoken")
    paths = [
        f"/rooms/{w.room.id}",
        f"/rooms/{w.room.id}/@{message.id}",
        f"/rooms/{w.room.id}/messages/{message.id}",
        f"/rooms/{w.room.id}/messages/{message.id}/edit",
        f"/rooms/{w.room.id}/involvement",
        "/rooms/opens/new",
        "/rooms/closeds/new",
        "/rooms/directs/new",
        f"/rooms/opens/{w.room.id}/edit",
        f"/messages/{message.id}/boosts",
        f"/messages/{message.id}/boosts/new",
        "/users/me/sidebar",
        "/users/me/profile",
        f"/users/{w.member.id}",
        "/users/me/push_subscriptions",
        "/account/edit",
        "/account/bots",
        "/account/bots/new",
        f"/account/bots/{bot.id}/edit",
        "/account/custom_styles/edit",
        "/searches",
        "/searches?q=coffee",
    ]
    for path in paths:
        response = w.client.get(path)
        assert response.status_code == 200, (path, response.status_code, response.text[:300])
        assert response.headers["content-type"].startswith("text/html"), path
        assert response.text.rstrip().endswith("</html>"), path


def test_welcome_redirects_to_last_room(w: World) -> None:
    w.client.get(f"/rooms/{w.private.id}")
    assert w.client.get("/").headers["location"] == f"/rooms/{w.private.id}"
    with TestClient(app, follow_redirects=False) as fresh:
        w.login(fresh, w.member)
        response = fresh.get("/")
        assert response.status_code == 302
        assert response.headers["location"] == f"/rooms/{w.room.id}"


def test_anonymous_pages_and_redirects(w: World) -> None:
    with TestClient(app, follow_redirects=False) as anonymous:
        assert anonymous.get("/").headers["location"] == "/session/new"
        assert anonymous.get(f"/rooms/{w.room.id}").headers["location"] == "/session/new"
        assert anonymous.get("/session/new").status_code == 200
        assert anonymous.get("/first_run").headers["location"] == "/"
        assert anonymous.get("/join/invitation").status_code == 200
        assert anonymous.get("/join/wrong").status_code == 404
        up = anonymous.get("/up")
        assert up.text == (
            '<!doctype html><html><body style="background-color: green"></body></html>'
        )
        manifest = anonymous.get("/webmanifest.json")
        assert manifest.headers["content-type"].startswith("application/manifest+json")
        assert manifest.json()["name"] == "Campfire"
        assert anonymous.get("/service-worker.js").status_code == 200
        assert anonymous.get("/account/logo").headers["content-type"] == "image/png"
        qr = anonymous.get("/qr_code/" + rails.b64(b"https://example.test").replace("/", "_"))
        assert qr.headers["content-type"] == "image/png"


def test_static_assets_are_digested_and_immutable(w: World) -> None:
    page = w.client.get(f"/rooms/{w.room.id}").text
    match = re.search(r'<link rel="stylesheet" href="(/assets/[^"]+\.css)"', page)
    assert match
    css = w.client.get(match[1])
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert css.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert b"{" in css.content
    assert w.client.get("/assets/../db/production.sqlite3").status_code == 404


def test_signup_through_join_code(w: World) -> None:
    with TestClient(app, follow_redirects=False) as fresh:
        response = fresh.post(
            "/join/invitation",
            data={
                "user[name]": "Joined",
                "user[email_address]": "joined@example.test",
                "user[password]": "secret",
            },
        )
        assert response.status_code == 302
        assert fresh.get(f"/rooms/{w.room.id}").status_code == 200


def test_profile_account_and_custom_styles_persist(w: World) -> None:
    assert w.post("/users/me/profile", {"user[bio]": "Profile bio"}, "patch").status_code == 302
    assert b"Profile bio" in w.client.get("/users/me/profile").content
    assert w.post("/account", {"account[name]": "Renamed"}, "patch").status_code == 302
    assert b'value="Renamed"' in w.client.get("/account/edit").content
    response = w.post(
        "/account/custom_styles", {"account[custom_styles]": "body { --x: 1; }"}, "patch"
    )
    assert response.status_code == 302
    assert b"<style>body { --x: 1; }</style>" in w.client.get("/account/edit").content
    old = w.scalar("SELECT join_code FROM accounts")
    assert w.post("/account/join_code").status_code == 302
    assert w.scalar("SELECT join_code FROM accounts") != old


def test_member_cannot_administer_account(w: World) -> None:
    with TestClient(app, follow_redirects=False) as other:
        token = w.login(other, w.member)
        assert w.post("/account", {"account[name]": "x"}, "patch", other, token).status_code == 403
        assert other.get("/account/bots").status_code == 403


def test_rooms_create_rename_involvement_and_delete(w: World) -> None:
    response = w.post(
        "/rooms/closeds",
        {"room[name]": "Secret plans", "user_ids[]": [str(w.member.id)]},
    )
    assert response.status_code == 302
    room_id = int(response.headers["location"].rsplit("/", 1)[1])
    members = w.sql("SELECT user_id FROM memberships WHERE room_id=? ORDER BY user_id", room_id)
    assert members == [(w.admin.id,), (w.member.id,)]
    response = w.post(f"/rooms/closeds/{room_id}", {"room[name]": "Renamed plans"}, "patch")
    assert response.status_code == 302
    assert w.scalar("SELECT name FROM rooms WHERE id=?", room_id) == "Renamed plans"
    response = w.client.put(
        f"/rooms/{room_id}/involvement", data={"involvement": "everything"}
    )
    assert response.status_code == 200
    assert w.scalar(
        "SELECT involvement FROM memberships WHERE room_id=? AND user_id=?", room_id, w.admin.id
    ) == "everything"
    assert w.post(f"/rooms/{room_id}", method="delete").status_code == 302
    assert w.scalar("SELECT count(*) FROM rooms WHERE id=?", room_id) == 0


def test_restricted_room_creation(w: World) -> None:
    w.post(
        "/account",
        {"account[settings][restrict_room_creation_to_administrators]": "true"},
        "patch",
    )
    with TestClient(app, follow_redirects=False) as other:
        token = w.login(other, w.member)
        assert w.post("/rooms/opens", {"room[name]": "x"}, client=other, token=token).status_code == 403


def test_bots_lifecycle_and_key_rotation(w: World) -> None:
    response = w.post(
        "/account/bots", {"user[name]": "Deploy bot", "user[webhook_url]": "https://example.test/"}
    )
    assert response.status_code == 302
    bot_id = w.scalar("SELECT id FROM users WHERE role=2")
    assert isinstance(bot_id, int)
    assert w.scalar("SELECT url FROM webhooks WHERE user_id=?", bot_id) == "https://example.test/"
    token = w.scalar("SELECT bot_token FROM users WHERE id=?", bot_id)
    assert w.client.put(f"/account/bots/{bot_id}/key").status_code == 302
    assert w.scalar("SELECT bot_token FROM users WHERE id=?", bot_id) != token
    listing = w.client.get("/account/bots").text
    assert "Deploy bot" in listing
    assert w.post(f"/account/bots/{bot_id}", method="delete").status_code == 302
    assert w.scalar("SELECT status FROM users WHERE id=?", bot_id) == 1


def test_autocomplete_json_and_html(w: World) -> None:
    response = w.client.get(
        f"/autocompletable/users?query=mem&room_id={w.room.id}",
        headers={"accept": "application/json"},
    )
    assert [user["name"] for user in response.json()] == ["Member"]
    html = w.client.get("/autocompletable/users?query=oth").text
    assert "Other" in html
    assert "Member" not in html


def test_refresh_returns_new_and_updated_messages(w: World) -> None:
    first = w.message(body="before refresh")
    since = int(first.created_at.timestamp() * 1000)
    second = w.message(body="after refresh")
    response = w.client.get(f"/rooms/{w.room.id}/refresh?since={since}")
    assert response.headers["content-type"].startswith("text/vnd.turbo-stream.html")
    assert f'data-message-id="{second.id}"' in response.text
    assert 'action="append"' in response.text


def test_message_paging_json_and_not_found(w: World) -> None:
    messages = [w.message(body=f"page {i}") for i in range(45)]
    page = w.client.get(f"/rooms/{w.room.id}/messages?before={messages[-1].id}").text
    ids = [int(i) for i in re.findall(r'data-message-id="(\d+)"', page)]
    assert ids == [m.id for m in messages[4:44]]
    after = w.client.get(f"/rooms/{w.room.id}/messages?after={messages[0].id}").text
    ids = [int(i) for i in re.findall(r'data-message-id="(\d+)"', after)]
    assert ids == [m.id for m in messages[1:41]]
    assert w.client.get(f"/rooms/{w.room.id}/messages?before=999999").status_code == 404
    response = w.client.get(
        f"/rooms/{w.room.id}/messages/{messages[0].id}", headers={"accept": "application/json"}
    )
    assert response.json()["body"]["plain_text"] == "page 0"


def test_searches_record_recent_queries_and_clear(w: World) -> None:
    response = w.post("/searches", {"q": "coffee!"})
    assert response.headers["location"] == "/searches?q=coffee"
    assert w.scalar("SELECT query FROM searches") == "coffee"
    assert w.post("/searches/clear", method="delete").status_code == 302
    assert w.scalar("SELECT count(*) FROM searches") == 0


def test_ban_and_unban(w: World) -> None:
    with TestClient(app, follow_redirects=False) as other:
        w.login(other, w.other)
    assert w.post(f"/users/{w.other.id}/ban").status_code == 302
    assert w.scalar("SELECT status FROM users WHERE id=?", w.other.id) == 2
    assert w.scalar("SELECT count(*) FROM bans WHERE user_id=?", w.other.id) == 1
    assert w.scalar("SELECT count(*) FROM sessions WHERE user_id=?", w.other.id) == 0
    assert w.post(f"/users/{w.other.id}/ban", method="delete").status_code == 302
    assert w.scalar("SELECT status FROM users WHERE id=?", w.other.id) == 0


def test_deactivate_and_role_changes(w: World) -> None:
    response = w.post(f"/account/users/{w.member.id}", {"user[role]": "administrator"}, "patch")
    assert response.status_code == 302
    assert w.scalar("SELECT role FROM users WHERE id=?", w.member.id) == 1
    assert w.post(f"/account/users/{w.other.id}", method="delete").status_code == 302
    email = w.scalar("SELECT email_address FROM users WHERE id=?", w.other.id)
    assert isinstance(email, str)
    assert "-deactivated-" in email


def test_logout_clears_session(w: World) -> None:
    response = w.post("/session", method="delete")
    assert response.status_code == 302
    assert w.client.get(f"/rooms/{w.room.id}").headers["location"] == "/session/new"
