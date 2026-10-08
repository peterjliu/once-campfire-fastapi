"""Native libvips/ffmpeg/poppler transforms with Rails variant-record storage ownership."""

import base64
import hashlib
import json
import secrets
import subprocess
import tempfile
import threading
from pathlib import Path, PurePath
from urllib.parse import quote

from . import config, db, rails
from .jsonvalue import as_dict, as_list
from .models import Blob
from .queries import attached_blob
from .storage import Upload, attach, checksum, create_blob, path_for, store_upload, write_file
from .vips import analyze_image, convert_to_webp, thumbnail

VARIABLE_TYPES = {
    "image/png",
    "image/gif",
    "image/jpeg",
    "image/tiff",
    "image/webp",
    "image/avif",
    "image/heic",
    "image/heif",
}
DEFAULT_SIZE = (1200, 800)
_slots = threading.BoundedSemaphore(4)

type Marshalable = bool | int | str | Symbol | list[Marshalable] | dict[str, Marshalable] | None


class Symbol(str):
    """A Ruby Symbol in Marshal output (variation digests hash Marshal bytes)."""

    __slots__ = ()


def _number(n: int) -> bytes:
    if n == 0:
        return b"\x00"
    if 0 < n < 123:
        return bytes([n + 5])
    if -124 < n < 0:
        return bytes([(n - 5) & 255])
    length = max(1, (n.bit_length() + 7) // 8)
    return bytes([length]) + n.to_bytes(length, "little", signed=n < 0)


def marshal(value: Marshalable) -> bytes:
    symbols: list[str] = []

    def sym(s: str) -> bytes:
        if s in symbols:
            return b";" + _number(symbols.index(s))
        symbols.append(s)
        encoded = s.encode()
        return b":" + _number(len(encoded)) + encoded

    def pack(v: Marshalable) -> bytes:
        if v is None:
            return b"0"
        if v is True:
            return b"T"
        if v is False:
            return b"F"
        if isinstance(v, int):
            return b"i" + _number(v)
        if isinstance(v, Symbol):
            return sym(v)
        if isinstance(v, str):
            raw = v.encode()
            return b'I"' + _number(len(raw)) + raw + _number(1) + sym("E") + b"T"
        if isinstance(v, list):
            return b"[" + _number(len(v)) + b"".join(pack(x) for x in v)
        return b"{" + _number(len(v)) + b"".join(sym(k) + pack(x) for k, x in v.items())

    return b"\x04\x08" + pack(value)


def default_format(blob: Blob) -> str:
    ext = PurePath(blob.filename).suffix.lstrip(".").lower()
    known = ("png", "jpeg", "jpg", "webp", "gif", "tiff", "avif", "heic", "heif")
    return ext if ext in known else "png"


def transformations(
    blob: Blob, size: tuple[int, int], format: str | None = None
) -> dict[str, Marshalable]:
    return {
        "format": Symbol(format) if format else default_format(blob),
        "resize_to_limit": [size[0], size[1]],
    }


def variation_digest(transforms: dict[str, Marshalable]) -> str:
    return base64.b64encode(hashlib.sha1(marshal(transforms)).digest()).decode()


def _existing_variant(blob_id: int, digest: str) -> Blob | None:
    row = db.one(
        "SELECT id FROM active_storage_variant_records WHERE blob_id=? AND variation_digest=?",
        (blob_id, digest),
    )
    if row is None:
        return None
    return attached_blob("ActiveStorage::VariantRecord", int(row[0]), "image")


def find_variant(blob: Blob, size: tuple[int, int], format: str | None = None) -> Blob | None:
    """An already processed variant (read-only; callable on the event loop)."""
    existing = _existing_variant(blob.id, variation_digest(transformations(blob, size, format)))
    return existing if existing is not None and path_for(existing.key).is_file() else None


def variant(blob: Blob, size: tuple[int, int], format: str | None = None) -> Blob:
    transforms = transformations(blob, size, format)
    digest = variation_digest(transforms)
    existing = _existing_variant(blob.id, digest)
    if existing is not None and path_for(existing.key).is_file():
        return existing
    if any(v <= 0 or v > 16384 for v in size):
        raise ValueError("invalid transform dimensions")
    fmt = str(transforms["format"])
    with _slots:
        raw, width, height = thumbnail(path_for(blob.key), size, fmt)
    with db.transaction():
        record = db.one(
            "SELECT id FROM active_storage_variant_records WHERE blob_id=? AND variation_digest=?",
            (blob.id, digest),
        )
        if record is not None:
            attached = attached_blob("ActiveStorage::VariantRecord", int(record[0]), "image")
            if attached is not None and path_for(attached.key).is_file():
                return attached
            record_id = int(record[0])
        else:
            record_id = db.run(
                "INSERT INTO active_storage_variant_records(blob_id,variation_digest) VALUES (?,?)",
                (blob.id, digest),
            )
        output = create_blob(
            secrets.token_hex(14),
            PurePath(blob.filename).stem + "." + fmt,
            "image/" + ("jpeg" if fmt == "jpg" else fmt),
            len(raw),
            checksum(raw),
            json.dumps({"identified": True, "analyzed": True, "width": width, "height": height}),
        )
        write_file(output.key, raw)
        attach(output.id, "ActiveStorage::VariantRecord", record_id, "image")
        return output


def analyze(blob: Blob) -> dict[str, object]:
    metadata: dict[str, object] = {"identified": True, "analyzed": True}
    content_type = blob.content_type or ""
    if content_type in VARIABLE_TYPES:
        width, height = analyze_image(path_for(blob.key))
        metadata.update(width=width, height=height)
    elif content_type.startswith(("audio/", "video/")):
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path_for(blob.key)),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        probe = as_dict(json.loads(result.stdout)) or {}
        details = as_dict(probe.get("format")) or {}
        duration = details.get("duration", 0)
        metadata["duration"] = float(duration) if isinstance(duration, (str, int, float)) else 0.0
        for stream in as_list(probe.get("streams")) or []:
            fields = as_dict(stream)
            if fields is not None and fields.get("codec_type") == "video":
                metadata.update(width=fields.get("width"), height=fields.get("height"), angle=0)
    blob.metadata = json.dumps(metadata)
    db.run("UPDATE active_storage_blobs SET metadata=? WHERE id=?", (blob.metadata, blob.id))
    return metadata


def _render_preview(blob: Blob, directory: Path) -> bytes | None:
    content_type = blob.content_type or ""
    output = directory / "preview.webp"
    if content_type.startswith("video/"):
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                str(path_for(blob.key)),
                "-y",
                "-vframes",
                "1",
                "-vf",
                "thumbnail,scale=1200:800:force_original_aspect_ratio=decrease",
                str(output),
            ],
            check=True,
            timeout=60,
            capture_output=True,
        )
        return output.read_bytes()
    if content_type == "application/pdf":
        subprocess.run(
            [
                "pdftoppm",
                "-f",
                "1",
                "-singlefile",
                "-png",
                "-scale-to",
                "1200",
                str(path_for(blob.key)),
                str(directory / "preview"),
            ],
            check=True,
            timeout=60,
            capture_output=True,
        )
        return convert_to_webp(directory / "preview.png")
    return None


def preview(blob: Blob) -> Blob | None:
    existing = attached_blob("ActiveStorage::Blob", blob.id, "preview_image")
    if existing is not None:
        return existing
    working = config.settings.storage_path / "tmp"
    working.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=working) as directory:
        raw = _render_preview(blob, Path(directory))
    if raw is None:
        return None
    upload = Upload(PurePath(blob.filename).stem + ".webp", "image/webp", raw)
    return store_upload(upload, "ActiveStorage::Blob", blob.id, "preview_image")


def process_attachment(blob: Blob) -> Blob | None:
    analyze(blob)
    content_type = blob.content_type or ""
    if content_type in VARIABLE_TYPES:
        return variant(blob, DEFAULT_SIZE)
    if content_type.startswith("video/") or content_type == "application/pdf":
        return preview(blob)
    return None


def representation_url(
    blob: Blob, size: tuple[int, int] | None = None, format: str | None = None
) -> str:
    transforms = transformations(blob, size or DEFAULT_SIZE, format)
    variation = rails.sign(transforms, "ActiveStorage", "variation")
    return (
        "/rails/active_storage/representations/redirect/"
        + rails.signed_id("ActiveStorage::Blob", blob.id, "blob_id")
        + "/"
        + variation
        + "/"
        + quote(blob.filename)
    )
