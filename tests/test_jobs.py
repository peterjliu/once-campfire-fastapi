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
        queued = [json.loads(row[0])["kind"] for row in connection.execute("SELECT payload FROM jobs")]
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
        with patch("os.sched_getaffinity", return_value={2, 4}):
            assert worker_count() == 2
        with patch("os.sched_getaffinity", return_value=set(range(64))):
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
