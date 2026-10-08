"""Message writes: atomic creation, FTS, unread markers, search and publications."""

import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from unittest.mock import MagicMock, patch

import pytest

from campfire import db, domain, rails, rendering
from campfire.clock import format_time
from campfire.queries import get_message
from tests.conftest import World


def published(publish: MagicMock) -> list[tuple[str, object]]:
    batches: list[list[tuple[str, object]]] = [call.args[0] for call in publish.call_args_list]
    return [item for batch in batches for item in batch]


def test_live_message_append_uses_messages_controller_scrolling(w: World) -> None:
    message = w.message(body="live scroll regression")
    with patch("campfire.cable.publish_many") as publish:
        domain.publish_message(message, "append")
    rendered = published(publish)[0][1]
    assert isinstance(rendered, str)
    assert 'action="append"' in rendered
    assert "live scroll regression" in rendered
    assert "maintain_scroll" not in rendered


def test_search_reaches_sparse_membership_and_orders_newest_ids(w: World) -> None:
    visible = w.message(user=w.member, body="sparseneedle")
    now = format_time(visible.created_at)
    with sqlite3.connect(w.database) as connection:
        ids: list[int] = []
        for i in range(1100):
            cursor = connection.execute(
                "INSERT INTO messages(client_message_id,created_at,creator_id,room_id,updated_at)"
                " VALUES (?,?,?,?,?)",
                (f"hidden-{i}", now, w.admin.id, w.private.id, now),
            )
            assert cursor.lastrowid is not None
            ids.append(cursor.lastrowid)
        connection.executemany(
            "INSERT INTO message_search_index(rowid,body) VALUES (?,?)",
            [(i, "sparseneedle") for i in ids],
        )
    assert domain.search_message_ids(w.member, "sparseneedle") == [visible.id]
    w.foreign_write("DELETE FROM memberships WHERE user_id=? AND room_id=?", w.member.id, w.room.id)
    assert domain.search_message_ids(w.member, "sparseneedle") == []
    domain.grant_memberships(w.private, [w.member])
    newest = ids[-100:][::-1]
    w.foreign_write("UPDATE messages SET created_at='2000-01-01 00:00:00' WHERE id=?", ids[-1])
    assert domain.search_message_ids(w.member, "sparseneedle") == newest
    assert domain.search_message_ids(w.member, "sparseneedle AND") == []


def test_direct_lookup_requires_the_exact_member_set(w: World) -> None:
    larger = domain.create_room("Rooms::Direct", w.admin)
    domain.grant_memberships(larger, [w.admin, w.member, w.other])
    exact = domain.create_room("Rooms::Direct", w.admin)
    domain.grant_memberships(exact, [w.admin, w.member])
    response = w.post("/rooms/directs", {"user_ids[]": [str(w.member.id), str(w.member.id)]})
    assert response.status_code == 302
    assert response.headers["location"] == f"/rooms/{exact.id}"
    assert w.scalar("SELECT count(*) FROM rooms WHERE type='Rooms::Direct'") == 2


def test_populated_room_history_sidebar_search_are_real_pages(w: World) -> None:
    message = w.message()
    for path in (f"/rooms/{w.room.id}", f"/rooms/{w.room.id}/messages", "/searches?q=coffee"):
        response = w.client.get(path)
        assert response.status_code == 200
        assert f'data-message-id="{message.id}"'.encode() in response.content
        assert b"Coffee" in response.content
    sidebar = w.client.get("/users/me/sidebar").content
    assert b"All Talk" in sidebar
    assert b"<!DOCTYPE html>" in sidebar
    assert b"</html>" in sidebar
    assert f'name="current-user-id" content="{w.admin.id}"'.encode() in sidebar


def test_empty_batches_and_no_notification_recipients_do_no_io(w: World) -> None:
    from campfire import cable, jobs

    with patch("campfire.cable.redis_client") as redis, patch("campfire.jobs.connect") as connect:
        cable.publish_many([])
        jobs.enqueue_many([])
        w.foreign_write("UPDATE memberships SET involvement='nothing' WHERE room_id=?", w.room.id)
        message = domain.insert_bare_message(w.room, w.admin, "no-recipients")
        domain.enqueue_notifications(message, "ordinary body")
    redis.assert_not_called()
    connect.assert_not_called()


def test_cable_batch_preserves_every_stream_and_message_in_order() -> None:
    import orjson

    from campfire import cable

    client = MagicMock()
    pipeline = client.pipeline.return_value.__enter__.return_value
    publications: list[tuple[str, object]] = [
        ("room", "<b>first</b>"),
        ("user_2_unreads", {"roomId": 1}),
    ]
    with patch("campfire.cable.redis_client", return_value=client):
        cable.publish_many(publications)
    client.pipeline.assert_called_once_with(transaction=False)
    assert [{"stream": s, "message": v} for s, v in publications] == [
        orjson.loads(call.args[1]) for call in pipeline.publish.call_args_list
    ]
    assert all(call.args[0] == "campfire:cable" for call in pipeline.publish.call_args_list)
    pipeline.execute.assert_called_once_with()


def test_notification_batch_keeps_individual_jobs_and_one_queue_commit(w: World) -> None:
    from contextlib import contextmanager

    from campfire import jobs

    w.foreign_write("UPDATE memberships SET involvement='everything' WHERE room_id=?", w.room.id)
    message = domain.insert_bare_message(w.room, w.admin, "queue-batch")
    statements: list[str] = []
    original = jobs.connect

    @contextmanager
    def traced():
        with original() as connection:
            connection.set_trace_callback(statements.append)
            try:
                yield connection
            finally:
                connection.set_trace_callback(None)

    with patch("campfire.jobs.connect", side_effect=traced) as connect:
        domain.enqueue_notifications(message, "plain message")
    assert connect.call_count == 1
    assert statements.count("COMMIT") == 1
    import json

    with jobs.connect() as connection:
        payloads = [
            json.loads(row[0]) for row in connection.execute("SELECT payload FROM jobs ORDER BY id")
        ]
    assert payloads == [
        {"kind": "push", "data": {"user_id": user.id, "message_id": message.id}}
        for user in (w.member, w.other)
    ]


def test_queue_batch_failure_rolls_back_all_jobs(w: World) -> None:
    from campfire import jobs

    with jobs.connect() as connection:
        connection.execute(
            "CREATE TRIGGER reject_second_job BEFORE INSERT ON jobs "
            "WHEN json_extract(NEW.payload, '$.data.number')=2 "
            "BEGIN SELECT RAISE(ABORT, 'second job rejected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        jobs.enqueue_many([("test", {"number": 1}), ("test", {"number": 2})])
    with jobs.connect() as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_creation_reuses_one_fragment_for_cable_and_http(w: World) -> None:
    calls: list[str] = []
    original = rendering.render

    def counted(name: str, data: object) -> str:
        calls.append(name)
        return original(name, data)

    statements: list[str] = []
    with (
        patch("campfire.cable.publish_many") as publish,
        patch("campfire.rendering.render", side_effect=counted),
        db.trace(statements.append),
    ):
        response = w.post(
            f"/rooms/{w.room.id}/messages",
            {
                "message[body]": "fresh <strong>coffee</strong>",
                "message[client_message_id]": "one-fragment",
            },
        )
    assert response.status_code == 200
    assert response.text == published(publish)[0][1]
    assert calls == ["message"]
    sql = [statement.upper() for statement in statements]
    assert not any("DELETE FROM MESSAGE_SEARCH_INDEX" in query for query in sql)
    for table in ("ACTION_TEXT_RICH_TEXTS", "ACTIVE_STORAGE_ATTACHMENTS", "BOOSTS"):
        assert not any(query.startswith("SELECT") and table in query for query in sql), table
    assert b"message_one-fragment" in response.content
    assert b"fresh <strong>coffee</strong>" in response.content


def test_creation_keeps_shared_first_unread_and_direct_latest_unread(w: World) -> None:
    for kind in ("Rooms::Open", "Rooms::Closed", "Rooms::Direct"):
        w.foreign_write("UPDATE rooms SET type=? WHERE id=?", kind, w.room.id)
        w.foreign_write("UPDATE memberships SET unread_at=NULL WHERE room_id=?", w.room.id)
        room = domain.require_room(w.room.id)
        first = w.message(room=room, body="first")
        second = w.message(room=room, body="second")
        marker = w.scalar(
            "SELECT unread_at FROM memberships WHERE user_id=? AND room_id=?", w.member.id, room.id
        )
        expected = second if kind == "Rooms::Direct" else first
        assert marker == format_time(expected.created_at), kind


def before_begin(w: World, action: Callable[[], None]) -> AbstractContextManager[object]:
    original = db.begin

    def begin(connection: sqlite3.Connection) -> None:
        action()
        original(connection)

    return patch("campfire.db.begin", side_effect=begin)


def test_creation_rechecks_membership_immediately_before_begin(w: World) -> None:
    def revoke() -> None:
        w.foreign_write(
            "DELETE FROM memberships WHERE user_id=? AND room_id=?", w.admin.id, w.room.id
        )

    with before_begin(w, revoke), patch("campfire.cable.publish_many") as publish:
        response = w.post(
            f"/rooms/{w.room.id}/messages", {"message[body]": "revoked immediately before BEGIN"}
        )
    assert response.status_code == 422
    assert w.scalar("SELECT count(*) FROM messages") == 0
    assert w.scalar("SELECT count(*) FROM action_text_rich_texts") == 0
    publish.assert_not_called()


def test_creation_resolves_mention_name_inside_transaction(w: World) -> None:
    body = (
        '<p>Hello <action-text-attachment sgid="'
        + rails.sgid("User", w.member.id)
        + '"></action-text-attachment></p>'
    )

    def rename() -> None:
        w.foreign_write("UPDATE users SET name='Current mention' WHERE id=?", w.member.id)

    with before_begin(w, rename):
        message = w.message(body=body)
    assert (
        w.scalar("SELECT body FROM message_search_index WHERE rowid=?", message.id)
        == "Hello @Current mention"
    )
    assert message.fragment is not None
    assert 'title="Current mention"' in message.fragment


def test_creation_rolls_back_message_richtext_and_fts_when_unread_fails(w: World) -> None:
    w.foreign_write(
        "CREATE TRIGGER reject_unread BEFORE UPDATE OF unread_at ON memberships "
        "BEGIN SELECT RAISE(ABORT, 'unread update rejected'); END"
    )
    before = w.scalar("SELECT updated_at FROM rooms WHERE id=?", w.room.id)
    with patch("campfire.cable.publish_many") as publish, pytest.raises(sqlite3.IntegrityError):
        w.message(body="atomic needle")
    assert w.scalar("SELECT count(*) FROM messages") == 0
    assert w.scalar("SELECT count(*) FROM action_text_rich_texts") == 0
    assert domain.search_message_ids(w.admin, "needle") == []
    assert w.scalar("SELECT updated_at FROM rooms WHERE id=?", w.room.id) == before
    publish.assert_not_called()


def test_writes_update_fts_touch_room_and_mark_only_disconnected_members_unread(w: World) -> None:
    from campfire.clock import now

    w.foreign_write(
        "UPDATE memberships SET connected_at=?, connections=1 WHERE user_id=? AND room_id=?",
        format_time(now()),
        w.member.id,
        w.room.id,
    )
    response = w.post(
        f"/rooms/{w.room.id}/messages",
        {"message[body]": "fresh coffee", "message[client_message_id]": "from-browser"},
    )
    assert response.status_code == 200
    message_id = w.scalar("SELECT id FROM messages WHERE client_message_id='from-browser'")
    assert isinstance(message_id, int)
    assert b"message_from-browser" in response.content
    unread = "SELECT unread_at FROM memberships WHERE user_id=? AND room_id=?"
    assert w.scalar(unread, w.other.id, w.room.id) is not None
    assert w.scalar(unread, w.member.id, w.room.id) is None
    assert w.scalar("SELECT body FROM message_search_index WHERE rowid=?", message_id) == (
        "fresh coffee"
    )
    path = f"/rooms/{w.room.id}/messages/{message_id}"
    assert w.post(path, {"message[body]": "new tea"}, "patch").status_code == 302
    assert b"fresh coffee" not in w.client.get("/searches?q=coffee").content
    assert b"new tea" in w.client.get("/searches?q=tea").content
    assert w.post(path, method="delete").status_code == 200
    assert get_message(message_id) is None
    assert w.scalar("SELECT count(*) FROM message_search_index WHERE rowid=?", message_id) == 0


def test_human_blank_message_allowed_and_scripts_sanitized(w: World) -> None:
    assert w.post(f"/rooms/{w.room.id}/messages", {"message[body]": ""}).status_code == 200
    message = w.message(body='<script>alert(1)</script><a href="javascript:alert(2)">safe</a>')
    body = w.scalar(
        "SELECT body FROM action_text_rich_texts WHERE record_type='Message' AND record_id=?",
        message.id,
    )
    assert isinstance(body, str)
    assert "script" not in body
    assert "javascript" not in body


def test_new_user_granted_open_room_membership(w: World) -> None:
    user = domain.create_user(name="Joined", email_address="joined@example.test", password="secret")
    query = "SELECT count(*) FROM memberships WHERE user_id=? AND room_id=?"
    assert w.scalar(query, user.id, w.room.id) == 1
    assert w.scalar(query, user.id, w.private.id) == 0


def test_signed_inline_embeds_persist_and_mention_plaintext_is_indexed(w: World) -> None:
    from campfire.richtext import plain_text
    from campfire.richtext_render import render_body
    from campfire.storage import Upload, store_upload
    from tests.images import png

    blob = db.write(
        lambda: store_upload(
            Upload("inline.png", "image/png", png(20, 20)), "User", w.admin.id, "unused"
        )
    )
    body = (
        '<p>Hello <action-text-attachment sgid="'
        + rails.sgid("User", w.member.id)
        + '"></action-text-attachment> <action-text-attachment sgid="'
        + rails.sgid("ActiveStorage::Blob", blob.id)
        + '"></action-text-attachment></p>'
    )
    message = w.message(body=body)
    rich_id = w.scalar(
        "SELECT id FROM action_text_rich_texts WHERE record_type='Message' AND record_id=?",
        message.id,
    )
    assert (
        w.scalar(
            "SELECT count(*) FROM active_storage_attachments WHERE "
            "record_type='ActionText::RichText' AND record_id=? AND blob_id=?",
            rich_id,
            blob.id,
        )
        == 1
    )
    assert "@Member" in plain_text(body)
    assert "inline.png" in render_body(body)
    assert 'class="mention"' in render_body(body)
    response = w.client.get("/searches?q=Member")
    assert f'data-message-id="{message.id}"'.encode() in response.content
    corrupted = rails.sgid("ActiveStorage::Blob", blob.id)[:-1] + "0"
    assert "inline.png" not in render_body(
        '<action-text-attachment sgid="' + corrupted + '"></action-text-attachment>'
    )


def test_opengraph_rebuilt_safely_and_plain_links_linked() -> None:
    from campfire.richtext_render import render_body

    assert 'href="https://example.com/test"' in render_body("<p>https://example.com/test.</p>")
    content = (
        '<action-text-attachment content-type="application/vnd.actiontext.opengraph-embed" '
        'href="https://campfire.example.com/rooms/1" url="https://campfire.example.com/evil.png" '
        'filename="Title" caption="Description"></action-text-attachment>'
    )
    result = render_body(content, "campfire.example.com")
    assert "Title" in result
    assert "href=" not in result
    assert "<img" not in result


def test_independent_rails_editor_plain_text_vectors() -> None:
    import json
    from pathlib import Path

    from campfire.richtext import plain_text

    vectors = json.loads((Path(__file__).parent / "vectors/richtext.json").read_text())
    failures = [
        case["name"] for case in vectors["cases"] if plain_text(case["body"]) != case["plain_text"]
    ]
    assert failures == []
