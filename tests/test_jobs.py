"""Leased job queue, push endpoint policy, webhooks, backup/restore and launcher."""

import json
import os
from unittest.mock import patch

import httpx
import pytest

from campfire import cli, domain, jobs
from campfire.server import worker_count
from tests.conftest import World


def test_job_leases_recover_and_retry_is_bounded(w: World) -> None:
    jobs.enqueue("test", {"number": 1})
    job = jobs.claim()
    assert job is not None
    assert jobs.claim() is None
    with jobs.connect() as connection:
        connection.execute("UPDATE jobs SET lease_until=0")
    reclaimed = jobs.claim()
    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert reclaimed.token != job.token
    jobs.finish(job)
    assert jobs.claim() is None
    jobs.finish(reclaimed, RuntimeError("failed"))
    with jobs.connect() as connection:
        connection.execute("UPDATE jobs SET available_at=0,attempts=4")
    final = jobs.claim()
    assert final is not None
    jobs.finish(final, RuntimeError("last failure"))
    with jobs.connect() as connection:
        assert connection.execute("SELECT status FROM jobs").fetchone()[0] == "dead"


def test_push_endpoint_private_dns_and_lookalike_hosts_rejected() -> None:
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
        assert not jobs.permitted_endpoint("https://fcm.googleapis.com/message")
    assert not jobs.permitted_endpoint("https://fcm.googleapis.com.attacker.test/message")
    assert not jobs.permitted_endpoint("http://fcm.googleapis.com/message")


def test_webhook_reply_does_not_start_another_bot_webhook(w: World) -> None:
    one = domain.create_user(name="First bot", role=2, bot_token="one")
    two = domain.create_user(name="Second bot", role=2, bot_token="two")
    room = domain.create_room("Rooms::Direct", w.admin)
    domain.grant_memberships(room, [w.admin, one, two])
    w.foreign_write(
        "UPDATE memberships SET involvement='everything' WHERE room_id=? AND user_id=?",
        room.id,
        w.admin.id,
    )
    first = domain.create_webhook(one, "http://localhost/one")
    domain.create_webhook(two, "http://localhost/two")
    message = domain.create_message(w.admin, room, "hello")
    with jobs.connect() as connection:
        connection.execute("DELETE FROM jobs")
    response = httpx.Response(200, headers={"content-type": "text/plain"}, text="bot reply")
    with patch("httpx.post", return_value=response) as post:
        jobs.perform("webhook", {"webhook_id": first, "message_id": message.id})
    payload = post.call_args.kwargs["json"]
    assert payload["message"]["body"]["plain"] == "hello"
    assert payload["room"]["path"] == f"/rooms/{room.id}/{one.id}-one/messages"
    assert w.scalar("SELECT count(*) FROM messages WHERE room_id=?", room.id) == 2
    with jobs.connect() as connection:
        queued = [
            json.loads(row[0])["kind"] for row in connection.execute("SELECT payload FROM jobs")
        ]
    assert "webhook" not in queued
    assert "push" in queued


def test_backup_and_restore_preserve_rows_and_queue(w: World) -> None:
    message = w.message(body="backed up")
    jobs.enqueue("purge-blob", {"id": 123})
    cli.backup()
    w.foreign_write("DELETE FROM messages")
    with jobs.connect() as connection:
        connection.execute("DELETE FROM jobs")
    cli.restore()
    assert w.scalar("SELECT count(*) FROM messages WHERE id=?", message.id) == 1
    body = w.scalar(
        "SELECT body FROM action_text_rich_texts WHERE record_id=? AND record_type='Message'",
        message.id,
    )
    assert isinstance(body, str)
    assert "backed up" in body
    with jobs.connect() as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] > 0


def test_prepare_initializes_empty_and_rejects_unmigrated(empty_root: object) -> None:
    from campfire import config

    config.settings.database_path.unlink()
    cli.prepare()
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(config.settings.database_path)) as connection:
        versions = connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        assert versions > 0
        connection.execute("DELETE FROM schema_migrations")
        connection.commit()
    with pytest.raises(cli.PrepareError):
        cli.prepare()


def test_single_worker_needs_no_redis() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert worker_count() == 1


def test_redis_worker_default_respects_affinity_and_cap() -> None:
    with patch.dict(os.environ, {"REDIS_URL": "redis://localhost"}, clear=True):
        with patch("os.process_cpu_count", return_value=2):
            assert worker_count() == 2
        with patch("os.process_cpu_count", return_value=64):
            assert worker_count() == 4


def test_explicit_workers_require_redis_and_positive_count() -> None:
    with patch.dict(os.environ, {"WEB_WORKERS": "2"}, clear=True):
        with pytest.raises(ValueError, match="REDIS_URL"):
            worker_count()
        os.environ["REDIS_URL"] = "redis://localhost"
        assert worker_count() == 2
        os.environ["WEB_WORKERS"] = "0"
        with pytest.raises(ValueError, match="positive"):
            worker_count()


def test_push_transport_refuses_private_addresses_before_connecting() -> None:
    from campfire import push

    private = [(2, 1, 6, "", ("127.0.0.1", 443))]
    with (
        patch("socket.getaddrinfo", return_value=private),
        patch("socket.create_connection") as connect,
        pytest.raises(ValueError, match="private endpoint"),
    ):
        push.pinned_session().post("https://fcm.googleapis.com/send", data=b"payload")
    connect.assert_not_called()


def test_push_transport_connects_to_the_validated_address() -> None:
    from campfire import push

    public = [(2, 1, 6, "", ("142.250.1.1", 443))]
    with (
        patch("socket.getaddrinfo", return_value=public),
        patch("socket.create_connection", side_effect=OSError("stop")) as connect,
        pytest.raises(OSError, match="stop"),
    ):
        push.pinned_session().post("https://fcm.googleapis.com/send", data=b"payload", timeout=3.0)
    connect.assert_called_once_with(("142.250.1.1", 443), timeout=3.0)


def test_webhook_redirects_and_non_200_replies_are_not_followed_or_attached(w: World) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen.append(self.path)
            self.rfile.read(int(self.headers["content-length"]))
            if self.path == "/hook":
                self.send_response(302)
                self.send_header("location", "/elsewhere")
                self.send_header("content-type", "image/png")
                self.send_header("content-length", "5")
                self.end_headers()
                self.wfile.write(b"bytes")
                return
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", "8")
            self.end_headers()
            self.wfile.write(b"followed")

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        bot = domain.create_user(name="Redirect bot", role=2, bot_token="redirect")
        room = domain.create_room("Rooms::Direct", w.admin)
        domain.grant_memberships(room, [w.admin, bot])
        hook = domain.create_webhook(bot, f"http://127.0.0.1:{server.server_port}/hook")
        message = domain.create_message(w.admin, room, "hello")
        jobs.perform("webhook", {"webhook_id": hook, "message_id": message.id})
    finally:
        server.shutdown()
        server.server_close()
    assert seen == ["/hook"]
    assert w.scalar("SELECT count(*) FROM messages WHERE room_id=?", room.id) == 1


def test_queue_connection_is_reused_per_thread_and_reopened_after_reset(w: World) -> None:
    import threading

    with jobs.connect() as first:
        pass
    with jobs.connect() as second:
        assert second.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert second.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert first is second
    others: list[object] = []

    def elsewhere() -> None:
        with jobs.connect() as connection:
            others.append(connection)

    thread = threading.Thread(target=elsewhere)
    thread.start()
    thread.join()
    assert others[0] is not second
    jobs.reset()
    with jobs.connect() as reopened:
        assert reopened is not second
        jobs.enqueue("test", {"number": 1})
        assert reopened.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_restore_reopens_queue_connections(w: World) -> None:
    jobs.enqueue("kept", {"number": 1})
    cli.backup()
    with jobs.connect() as before:
        before.execute("DELETE FROM jobs")
    cli.restore()
    with jobs.connect() as after:
        assert after is not before
        assert after.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_webhook_may_target_private_network_bots_like_rails(w: World) -> None:
    """Rails deliberately exempts webhooks from PrivateNetworkGuard: only an administrator
    sets the URL, and self-hosted bots commonly live on loopback or the LAN."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["content-length"]))
            self.send_response(200)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.send_header("content-length", "9")
            self.end_headers()
            self.wfile.write(b"lan reply")

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        bot = domain.create_user(name="LAN bot", role=2, bot_token="lan")
        room = domain.create_room("Rooms::Direct", w.admin)
        domain.grant_memberships(room, [w.admin, bot])
        hook = domain.create_webhook(bot, f"http://127.0.0.1:{server.server_port}/hook")
        message = domain.create_message(w.admin, room, "hello")
        jobs.perform("webhook", {"webhook_id": hook, "message_id": message.id})
    finally:
        server.shutdown()
        server.server_close()
    assert (
        w.scalar(
            "SELECT action_text_rich_texts.body FROM messages JOIN action_text_rich_texts ON "
            "action_text_rich_texts.record_id=messages.id AND "
            "action_text_rich_texts.record_type='Message' WHERE messages.room_id=? "
            "AND messages.creator_id=?",
            room.id,
            bot.id,
        )
        == "lan reply"
    )


def test_unreachable_webhook_fails_the_job_without_a_reply(w: World) -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    bot = domain.create_user(name="Offline bot", role=2, bot_token="offline")
    room = domain.create_room("Rooms::Direct", w.admin)
    domain.grant_memberships(room, [w.admin, bot])
    hook = domain.create_webhook(bot, f"http://127.0.0.1:{closed_port}/hook")
    message = domain.create_message(w.admin, room, "hello")
    with pytest.raises(httpx.ConnectError):
        jobs.perform("webhook", {"webhook_id": hook, "message_id": message.id})
    assert w.scalar("SELECT count(*) FROM messages WHERE room_id=?", room.id) == 1
