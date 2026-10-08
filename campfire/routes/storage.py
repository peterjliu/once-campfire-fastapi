"""Active Storage endpoints: direct uploads, disk service, blobs and representations."""

import json
from datetime import timedelta
from pathlib import PurePath

from fastapi import APIRouter, Request
from starlette.responses import Response

from .. import db, rails
from ..clock import now
from ..files import serve_file
from ..http import json_response, not_found, status
from ..jsonvalue import as_dict, as_list
from ..media import find_variant, preview, variant
from ..models import Blob, User
from ..queries import blob_by_key, blob_for
from ..request_context import RequestContext, context_of
from ..storage import checksum, create_blob, path_for, serving_attributes, upload_owner

router = APIRouter(include_in_schema=False)
MAX_UPLOAD = 50 * 1024 * 1024


def register_upload(user_id: int, data: dict[str, object]) -> Blob:
    import secrets

    size = data["byte_size"]
    if isinstance(size, bool) or not isinstance(size, (int, str)) or not str(size).isdigit():
        raise ValueError("invalid size")
    byte_size = int(size)
    if byte_size < 0 or byte_size > MAX_UPLOAD:
        raise ValueError("invalid size")
    filename = data["filename"]
    digest = data["checksum"]
    if not isinstance(filename, str) or not isinstance(digest, str):
        raise ValueError("invalid upload")
    content_type = data.get("content_type")
    metadata = as_dict(data.get("metadata")) or {}
    with db.transaction():
        return create_blob(
            secrets.token_hex(14),
            PurePath(filename).name,
            content_type
            if isinstance(content_type, str) and content_type
            else "application/octet-stream",
            byte_size,
            digest,
            json.dumps({**metadata, "campfire_upload_user_id": user_id}),
        )


@router.post("/rails/active_storage/direct_uploads")
async def direct_upload(request: Request) -> Response:
    ctx = context_of(request)
    if ctx.current_user is None:
        return status(401)
    data = as_dict((ctx.params.json or {}).get("blob"))
    if data is None:
        return status(422)
    try:
        blob = await db.awrite(register_upload, ctx.current_user.id, data)
    except ValueError, KeyError:
        return status(422)
    token = rails.sign(
        {
            "key": blob.key,
            "content_type": blob.content_type,
            "content_length": blob.byte_size,
            "checksum": blob.checksum,
        },
        "ActiveStorage",
        "blob_token",
        now() + timedelta(minutes=5),
    )
    return json_response(
        {
            "id": blob.id,
            "key": blob.key,
            "filename": blob.filename,
            "content_type": blob.content_type,
            "byte_size": blob.byte_size,
            "checksum": blob.checksum,
            "signed_id": rails.signed_id("ActiveStorage::Blob", blob.id, "blob_id"),
            "attachable_sgid": rails.sgid("ActiveStorage::Blob", blob.id),
            "direct_upload": {
                "url": f"{ctx.origin}/rails/active_storage/disk/{token}",
                "headers": {"Content-Type": blob.content_type},
            },
        }
    )


def verified_disk(token: str, purpose: str) -> dict[str, object]:
    try:
        data = as_dict(rails.verify(token, "ActiveStorage", purpose))
    except ValueError, TypeError:
        data = None
    if data is None or not isinstance(data.get("key"), str):
        not_found()
    return data


@router.put("/rails/active_storage/disk/{token}")
async def disk_upload(request: Request, token: str) -> Response:
    ctx = context_of(request)
    data = verified_disk(token, "blob_token")
    key = str(data["key"])
    try:
        target = path_for(key)
    except ValueError:
        not_found()
    if ctx.current_session is None or ctx.current_user is None:
        return status(401)
    blob = blob_by_key(key)
    owner = upload_owner(blob) if blob is not None else None
    if blob is None or (owner is not None and owner != ctx.current_user.id):
        return status(403)
    raw = ctx.body
    if len(raw) != data.get("content_length") or checksum(raw) != data.get("checksum"):
        return status(422)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return Response(status_code=204)


@router.get("/rails/active_storage/disk/{token}/{filename}")
@router.get("/rails/active_storage/disk/{token}")
async def disk_download(request: Request, token: str) -> Response:
    data = verified_disk(token, "blob_key")
    try:
        target = path_for(str(data["key"]))
    except ValueError:
        not_found()
    disposition = data.get("disposition")
    mime, kind = serving_attributes(
        str(data.get("content_type") or ""),
        disposition if isinstance(disposition, str) else "inline",
    )
    filename = data.get("filename")
    return serve_file(
        context_of(request), target, mime, filename if isinstance(filename, str) else "file", kind
    )


def message_reachable(user: User, record_type: str, record_id: int) -> bool:
    if record_type == "Message":
        return db.exists(
            "SELECT 1 FROM messages JOIN memberships ON memberships.room_id=messages.room_id "
            "WHERE messages.id=? AND memberships.user_id=?",
            (record_id, user.id),
        )
    if record_type == "ActionText::RichText":
        return db.exists(
            "SELECT 1 FROM action_text_rich_texts rt JOIN messages ON messages.id=rt.record_id "
            "JOIN memberships ON memberships.room_id=messages.room_id WHERE rt.id=? AND "
            "rt.record_type='Message' AND memberships.user_id=?",
            (record_id, user.id),
        )
    return record_type in ("User", "Account")


def blob_allowed(user: User, blob: Blob) -> bool:
    attachments = db.rows(
        "SELECT record_type,record_id FROM active_storage_attachments WHERE blob_id=?", (blob.id,)
    )
    if not attachments:
        # Existing Rails draft signed URLs stay usable; native drafts record their uploader.
        owner = upload_owner(blob)
        return owner is None or owner == user.id
    return any(message_reachable(user, str(row[0]), int(row[1])) for row in attachments)


def signed_blob(token: str) -> Blob:
    try:
        blob = blob_for(rails.verify_id("ActiveStorage::Blob", token, "blob_id"))
    except ValueError:
        blob = None
    if blob is None:
        not_found()
    return blob


async def serve_blob(ctx: RequestContext, token: str) -> Response:
    if ctx.current_user is None:
        return status(401)
    blob = signed_blob(token)
    if not blob_allowed(ctx.current_user, blob):
        return status(403)
    mime, disposition = serving_attributes(blob.content_type, ctx.query("disposition", "inline"))
    try:
        target = path_for(blob.key)
    except ValueError:
        not_found()
    return serve_file(ctx, target, mime, blob.filename, disposition)


@router.get("/rails/active_storage/blobs/redirect/{token}/{filename:path}")
@router.get("/rails/active_storage/blobs/proxy/{token}/{filename:path}")
async def blob(request: Request, token: str) -> Response:
    return await serve_blob(context_of(request), token)


def representation_allowed(user: User, blob: Blob) -> bool:
    return db.exists(
        "SELECT 1 FROM active_storage_attachments a JOIN messages ON messages.id=a.record_id "
        "JOIN memberships ON memberships.room_id=messages.room_id WHERE a.blob_id=? AND "
        "a.record_type='Message' AND memberships.user_id=?",
        (blob.id, user.id),
    ) or db.exists(
        "SELECT 1 FROM active_storage_attachments a JOIN action_text_rich_texts rt ON "
        "rt.id=a.record_id JOIN messages ON messages.id=rt.record_id JOIN memberships ON "
        "memberships.room_id=messages.room_id WHERE a.blob_id=? AND "
        "a.record_type='ActionText::RichText' AND rt.record_type='Message' AND "
        "memberships.user_id=?",
        (blob.id, user.id),
    )


def transform_request(variation: str) -> tuple[tuple[int, int], str | None]:
    transforms = as_dict(rails.verify(variation, "ActiveStorage", "variation"))
    if transforms is None or set(transforms) - {"format", "resize_to_limit"}:
        raise ValueError("unsupported transformation")
    size = as_list(transforms.get("resize_to_limit", [1200, 800]))
    if size is None or len(size) != 2:
        raise ValueError("invalid size")
    width, height = size
    if not isinstance(width, int) or not isinstance(height, int):
        raise ValueError("invalid size")
    fmt = transforms.get("format")
    return (width, height), fmt if isinstance(fmt, str) else None


def processed(source: Blob, size: tuple[int, int], fmt: str | None) -> Blob:
    """Preview (video/PDF) and variant generation; runs on the writer thread."""
    content_type = source.content_type or ""
    if content_type == "application/pdf" or content_type.startswith("video/"):
        image = preview(source)
        if image is None:
            raise ValueError("no preview")
        source = image
    return variant(source, size, fmt)


@router.get("/rails/active_storage/representations/redirect/{token}/{variation}/{filename:path}")
@router.get("/rails/active_storage/representations/proxy/{token}/{variation}/{filename:path}")
async def representation(request: Request, token: str, variation: str) -> Response:
    ctx = context_of(request)
    try:
        size, fmt = transform_request(variation)
    except ValueError, TypeError:
        not_found()
    source = signed_blob(token)
    if ctx.current_user is None:
        return status(401)
    if not representation_allowed(ctx.current_user, source):
        return status(403)
    ready = None
    if (
        not (source.content_type or "").startswith("video/")
        and source.content_type != "application/pdf"
    ):
        ready = find_variant(source, size, fmt)
    try:
        output = ready or await db.awrite(processed, source, size, fmt)
    except ValueError, FileNotFoundError:
        not_found()
    return serve_file(
        ctx, path_for(output.key), output.content_type or "image/png", output.filename
    )
