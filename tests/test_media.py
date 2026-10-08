"""Uploads, variants, previews, direct uploads and attachment transactions."""

import base64
import hashlib
import json
import subprocess
import wave
from io import BytesIO
from urllib.parse import urlsplit
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from campfire import domain, rails
from campfire.asgi import app
from campfire.media import representation_url, variant
from campfire.queries import blob_for, message_attachment_blob
from campfire.rendering import message_views
from campfire.storage import Upload, blob_url, path_for
from tests.conftest import World
from tests.images import pdf, png


def test_image_upload_creates_real_thumb_metadata_and_range_download(w: World) -> None:
    raw = png(1600, 1000)
    response = w.client.post(
        f"/rooms/{w.room.id}/messages",
        data={"message[body]": ""},
        files={"message[attachment]": ("picture.png", raw, "image/png")},
    )
    assert response.status_code == 200
    # Known upload objects retain analysis dimensions in the reused fragment.
    assert b"width: 600.0px; aspect-ratio: 1.6" in response.content
    message_id = w.scalar("SELECT max(id) FROM messages")
    assert isinstance(message_id, int)
    original = message_attachment_blob(message_id)
    assert original is not None
    assert json.loads(original.metadata or "{}")["width"] == 1600
    thumb = variant(original, (1200, 800))
    assert thumb.byte_size < original.byte_size
    assert json.loads(thumb.metadata or "{}")["width"] == 1200
    assert w.client.get(representation_url(original)).status_code == 200
    ranged = w.client.get(blob_url(original), headers={"range": "bytes=0-9"})
    assert ranged.status_code == 206
    assert ranged.content == raw[:10]
    with TestClient(app, follow_redirects=False) as member:
        w.login(member, w.member)
        w.foreign_write(
            "DELETE FROM memberships WHERE user_id=? AND room_id=?", w.member.id, w.room.id
        )
        assert member.get(blob_url(original)).status_code == 403


def test_attachment_transaction_failure_cleans_files_and_rows(w: World) -> None:
    with (
        patch("campfire.media.process_attachment", side_effect=RuntimeError("analysis failed")),
        pytest.raises(RuntimeError),
    ):
        domain.create_message(
            w.admin, w.room, attachment=Upload("picture.png", "image/png", png(10, 10))
        )
    assert w.scalar("SELECT count(*) FROM messages") == 0
    assert w.scalar("SELECT count(*) FROM active_storage_blobs") == 0
    assert not any(p.is_file() for p in w.files.rglob("*"))


def test_signed_direct_upload_and_tamper_rejection(w: World) -> None:
    raw = b"a direct upload"
    checksum = base64.b64encode(hashlib.md5(raw).digest()).decode()
    body = {
        "blob": {
            "filename": "document.txt",
            "byte_size": len(raw),
            "checksum": checksum,
            "content_type": "text/plain",
        }
    }
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as secure:
        secure.cookies = w.client.cookies
        response = secure.post(
            "/rails/active_storage/direct_uploads",
            content=json.dumps(body),
            headers={"content-type": "application/json", "sec-fetch-site": "same-origin"},
        )
    assert response.status_code == 200
    data = response.json()
    path = urlsplit(data["direct_upload"]["url"]).path
    plain = {"content-type": "text/plain"}
    with TestClient(app, follow_redirects=False) as anonymous:
        assert anonymous.put(path, content=raw, headers=plain).status_code == 401
        assert anonymous.put(path + "bad", content=raw, headers=plain).status_code == 404
    assert w.client.put(path, content=raw, headers=plain).status_code == 204
    blob = blob_for(data["id"])
    assert blob is not None
    assert w.client.get(blob_url(blob)).status_code == 200
    with TestClient(app, follow_redirects=False) as other:
        w.login(other, w.other)
        assert other.get(blob_url(blob)).status_code == 403
        assert other.put(path, content=raw, headers=plain).status_code == 403
    for metadata in ("[]", "123", '"legacy"', "not-json"):
        w.foreign_write("UPDATE active_storage_blobs SET metadata=? WHERE id=?", metadata, blob.id)
        assert w.client.put(path, content=raw, headers=plain).status_code == 204
    w.foreign_write(
        "UPDATE active_storage_blobs SET metadata=? WHERE id=?", blob.metadata, blob.id
    )
    response = w.post(
        f"/rooms/{w.room.id}/messages",
        {"message[body]": "", "message[attachment]": data["signed_id"]},
    )
    assert response.status_code == 200
    assert w.scalar(
        "SELECT count(*) FROM active_storage_attachments WHERE record_type='Message' AND blob_id=?",
        data["id"],
    ) == 1


def test_pdf_video_audio_native_processing_and_presentation(w: World) -> None:
    video = w.root / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:d=0.1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
        timeout=20,
    )
    audio = BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0" * 1600)
    for filename, raw, mime in (
        ("sample.pdf", pdf(400, 300), "application/pdf"),
        ("sample.mp4", video.read_bytes(), "video/mp4"),
        ("sample.wav", audio.getvalue(), "audio/wav"),
    ):
        message = domain.create_message(w.admin, w.room, "", attachment=Upload(filename, mime, raw))
        blob = message_attachment_blob(message.id)
        assert blob is not None
        rendered = str(message_views([message])[0].HTML)
        if mime == "audio/wav":
            assert "Download" in rendered
            assert json.loads(blob.metadata or "{}")["duration"] > 0
            continue
        assert w.scalar(
            "SELECT count(*) FROM active_storage_attachments WHERE "
            "record_type='ActiveStorage::Blob' AND record_id=? AND name='preview_image'",
            blob.id,
        ) == 1
        assert w.client.get(representation_url(blob)).status_code == 200
        assert ("poster=" if mime == "video/mp4" else "lightbox#open") in rendered


def test_first_run_preserves_avatar_and_redirects_to_room(w: World) -> None:
    w.foreign_write("DELETE FROM accounts")
    with TestClient(app, follow_redirects=False) as fresh:
        page = fresh.get("/first_run")
        assert page.status_code == 200
        assert b'name="csrf-token"' not in page.content
        response = fresh.post(
            "/first_run",
            data={
                "user[name]": "Founder",
                "user[email_address]": "founder@example.test",
                "user[password]": "secret",
            },
            files={"user[avatar]": ("avatar.png", png(10, 10), "image/png")},
        )
        assert response.status_code == 302
        founder = w.scalar("SELECT id FROM users WHERE email_address='founder@example.test'")
        assert w.scalar(
            "SELECT count(*) FROM active_storage_attachments WHERE "
            "record_type='User' AND record_id=? AND name='avatar'",
            founder,
        ) == 1
        location = fresh.get("/").headers["location"]
        assert location.startswith("/rooms/")
        assert location.removeprefix("/rooms/").isdigit()


def test_attachment_updates_preserve_body_index_and_rollback(w: World) -> None:
    message = domain.create_message(
        w.admin, w.room, "retained body", attachment=Upload("old.png", "image/png", png(30, 20))
    )
    old = message_attachment_blob(message.id)
    assert old is not None
    path = f"/rooms/{w.room.id}/messages/{message.id}"
    response = w.client.patch(
        path, files={"message[attachment]": ("new.png", png(30, 20), "image/png")}
    )
    assert response.status_code == 302
    body_query = (
        "SELECT body FROM action_text_rich_texts WHERE record_id=? AND record_type='Message'"
    )
    assert w.scalar(body_query, message.id) == "retained body"
    new = message_attachment_blob(message.id)
    assert new is not None
    assert new.id != old.id
    assert path_for(old.key).exists()
    assert (
        f'data-message-id="{message.id}"'.encode() in w.client.get("/searches?q=retained").content
    )
    before = {p for p in w.files.rglob("*") if p.is_file()}
    with (
        patch("campfire.media.process_attachment", side_effect=RuntimeError("analysis failed")),
        pytest.raises(RuntimeError),
    ):
        domain.update_message(
            message, attachment=Upload("failure.png", "image/png", png(30, 20))
        )
    current = message_attachment_blob(message.id)
    assert current is not None
    assert current.id == new.id
    assert {p for p in w.files.rglob("*") if p.is_file()} == before
    response = w.post(
        path,
        {
            "message[body]": "",
            "message[attachment]": rails.signed_id("ActiveStorage::Blob", old.id, "blob_id"),
        },
        "patch",
    )
    assert response.status_code == 302
    assert b"old.png" in w.client.get("/searches?q=old").content
    current = message_attachment_blob(message.id)
    assert current is not None
    assert current.id == old.id
    assert w.post(path, {"message[attachment]": ""}, "patch").status_code == 302
    assert message_attachment_blob(message.id) is None


def test_bot_multipart_update_preserves_omitted_body(w: World) -> None:
    bot = domain.create_user(name="Bot", role=2, bot_token="token")
    domain.grant_memberships(w.room, [bot])
    message = domain.create_message(bot, w.room, "bot body")
    with TestClient(app, follow_redirects=False) as anonymous:
        response = anonymous.patch(
            f"/rooms/{w.room.id}/{bot.id}-token/messages/{message.id}",
            files={"attachment": ("new.txt", b"file", "text/plain")},
        )
    assert response.status_code == 200
    assert (
        w.scalar(
            "SELECT body FROM action_text_rich_texts WHERE record_id=? AND record_type='Message'",
            message.id,
        )
        == "bot body"
    )
    blob = message_attachment_blob(message.id)
    assert blob is not None
    assert blob.filename == "new.txt"


def test_active_uploaded_content_downloads_as_binary_attachment(w: World) -> None:
    for mime, raw in (
        ("text/html", b"<script>alert(1)</script>"),
        (
            "image/svg+xml",
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        ),
    ):
        message = domain.create_message(w.admin, w.room, attachment=Upload("active.txt", mime, raw))
        blob = message_attachment_blob(message.id)
        assert blob is not None
        response = w.client.get(blob_url(blob))
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment;")


def test_avatar_variant_and_default_initials(w: World) -> None:
    signed = rails.signed_id("User", w.member.id, "avatar")
    response = w.client.get(f"/users/{signed}/avatar")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert b"\n      M\n    </text>" in response.content
    w.client.post(
        "/users/me/profile",
        data={"_method": "patch", "user[name]": "Admin"},
        files={"user[avatar]": ("me.png", png(600, 600), "image/png")},
    )
    signed = rails.signed_id("User", w.admin.id, "avatar")
    response = w.client.get(f"/users/{signed}/avatar")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.content[:4] == b"RIFF"
