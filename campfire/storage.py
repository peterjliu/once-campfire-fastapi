"""Active Storage keys, disk layout, uploads and ownership."""

import base64
import hashlib
import json
import mimetypes
import secrets
from dataclasses import dataclass
from pathlib import Path, PurePath
from urllib.parse import quote

from . import config, db, rails
from .clock import format_time, now
from .jsonvalue import as_dict
from .models import Blob
from .queries import blob_for

# Installed activestorage/lib/active_storage/engine.rb and Blob::Servable.
BINARY_TYPES = {
    "text/html",
    "image/svg+xml",
    "application/postscript",
    "application/x-shockwave-flash",
    "text/xml",
    "application/xml",
    "application/xhtml+xml",
    "application/mathml+xml",
    "text/cache-manifest",
}
INLINE_TYPES = {
    "image/webp",
    "image/avif",
    "image/png",
    "image/gif",
    "image/jpeg",
    "image/tiff",
    "image/bmp",
    "image/vnd.adobe.photoshop",
    "image/vnd.microsoft.icon",
    "application/pdf",
}


@dataclass(frozen=True, slots=True)
class Upload:
    filename: str
    content_type: str
    data: bytes


def serving_attributes(content_type: str | None, disposition: str) -> tuple[str, str]:
    mime = (content_type or "application/octet-stream").split(";", 1)[0].lower()
    if mime in BINARY_TYPES:
        return "application/octet-stream", "attachment"
    return mime, disposition if mime in INLINE_TYPES else "attachment"


def path_for(key: str) -> Path:
    if not key.isalnum() or len(key) < 4:
        raise ValueError("invalid storage key")
    return config.settings.files_path / key[:2] / key[2:4] / key


def blob_url(blob: Blob) -> str:
    token = rails.signed_id("ActiveStorage::Blob", blob.id, "blob_id")
    return "/rails/active_storage/blobs/redirect/" + token + "/" + quote(blob.filename)


def checksum(raw: bytes) -> str:
    return base64.b64encode(hashlib.md5(raw).digest()).decode()


def new_key() -> str:
    return secrets.token_urlsafe(21).replace("-", "a").replace("_", "b")


def create_blob(
    key: str,
    filename: str,
    content_type: str,
    byte_size: int,
    digest: str | None,
    metadata: str = "{}",
) -> Blob:
    created = now()
    blob_id = db.run(
        "INSERT INTO active_storage_blobs(byte_size,checksum,content_type,created_at,filename,"
        "key,metadata,service_name) VALUES (?,?,?,?,?,?,?,'local')",
        (byte_size, digest, content_type, format_time(created), filename, key, metadata),
    )
    return Blob(blob_id, key, filename, content_type, metadata, byte_size, digest, created)


def attach(blob_id: int, record_type: str, record_id: int, name: str) -> None:
    db.run(
        "INSERT INTO active_storage_attachments(blob_id,created_at,name,record_id,record_type) "
        "VALUES (?,?,?,?,?)",
        (blob_id, format_time(now()), name, record_id, record_type),
    )


def write_file(key: str, raw: bytes) -> Path:
    """Write a stored file inside the current transaction; rollback removes it."""
    target = path_for(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    tx = db.current()
    if tx is not None:
        tx.track_file(target)
    return target


def store_upload(upload: Upload, record_type: str, record_id: int, name: str) -> Blob:
    with db.transaction():
        filename = PurePath(upload.filename).name or "file"
        content_type = (
            upload.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )
        key = new_key()
        blob = create_blob(key, filename, content_type, len(upload.data), checksum(upload.data))
        write_file(key, upload.data)
        attach(blob.id, record_type, record_id, name)
        return blob


def attach_signed(token: str, record_type: str, record_id: int, name: str) -> Blob:
    blob = blob_for(rails.verify_id("ActiveStorage::Blob", token, "blob_id"))
    if blob is None or not path_for(blob.key).is_file():
        raise ValueError("upload missing")
    attach(blob.id, record_type, record_id, name)
    return blob


def upload_owner(blob: Blob) -> int | None:
    try:
        metadata = as_dict(json.loads(blob.metadata or "{}"))
    except ValueError, TypeError:
        return None
    owner = metadata.get("campfire_upload_user_id") if metadata is not None else None
    return owner if isinstance(owner, int) and not isinstance(owner, bool) else None


def blob_ids(record_type: str, record_id: int, name: str | None = None) -> list[int]:
    query = "SELECT blob_id FROM active_storage_attachments WHERE record_type=? AND record_id=?"
    params: list[db.Param] = [record_type, record_id]
    if name is not None:
        query += " AND name=?"
        params.append(name)
    return [int(row[0]) for row in db.rows(query, params)]


def remove_attachment(record_type: str, record_id: int, name: str) -> None:
    from . import jobs

    with db.transaction() as tx:
        ids = blob_ids(record_type, record_id, name)
        db.run(
            "DELETE FROM active_storage_attachments WHERE record_type=? AND record_id=? AND name=?",
            (record_type, record_id, name),
        )
        for blob_id in ids:
            tx.on_commit(lambda blob_id=blob_id: jobs.enqueue("purge", {"blob_id": blob_id}))


def replace_attachment(upload: Upload, record_type: str, record_id: int, name: str) -> Blob:
    with db.transaction():
        remove_attachment(record_type, record_id, name)
        return store_upload(upload, record_type, record_id, name)


def purge_blob(blob_id: int) -> None:
    """Delete an unattached blob, its variants/previews and their files."""
    if db.exists("SELECT 1 FROM active_storage_attachments WHERE blob_id=?", (blob_id,)):
        return
    blob = blob_for(blob_id)
    if blob is None:
        return
    children: list[int] = []
    with db.transaction():
        variants = [
            int(row[0])
            for row in db.rows(
                "SELECT id FROM active_storage_variant_records WHERE blob_id=?", (blob_id,)
            )
        ]
        for variant_id in variants:
            children += blob_ids("ActiveStorage::VariantRecord", variant_id)
            db.run(
                "DELETE FROM active_storage_attachments WHERE "
                "record_type='ActiveStorage::VariantRecord' AND record_id=?",
                (variant_id,),
            )
            db.run("DELETE FROM active_storage_variant_records WHERE id=?", (variant_id,))
        children += blob_ids("ActiveStorage::Blob", blob_id)
        db.run(
            "DELETE FROM active_storage_attachments WHERE record_type='ActiveStorage::Blob' "
            "AND record_id=?",
            (blob_id,),
        )
        db.run("DELETE FROM active_storage_blobs WHERE id=?", (blob_id,))
    path_for(blob.key).unlink(missing_ok=True)
    for child in children:
        purge_blob(child)
