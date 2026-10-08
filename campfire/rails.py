"""Rails wire formats, checked against independently generated Rails vectors."""

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from datetime import UTC, datetime
from functools import lru_cache
from urllib.parse import unquote, urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import clock, config
from .jsonvalue import as_dict

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None


def encode(value: object) -> bytes:
    """ActiveSupport JSON: HTML-significant characters are escaped."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").encode()


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode()


def decode64(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError("invalid base64")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, UnicodeError) as error:
        raise ValueError("invalid base64") from error


@lru_cache(maxsize=64)
def _derived_key(secret: str, salt: str, length: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), 1000, length)


def key(salt: str, length: int = 64) -> bytes:
    return _derived_key(config.settings.secret_key_base, salt, length)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _expiry(expiry: datetime) -> str:
    return expiry.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _marshal_string(raw: bytes) -> str:
    # Rails 7-era signed messages contained Marshal strings. Read only that scalar;
    # never instantiate classes or deserialize arbitrary Ruby objects.
    body = raw[2:]
    if body.startswith(b"I"):
        body = body[1:]
    if len(body) < 2 or body[0] != ord('"'):
        raise ValueError("unsupported Marshal value")
    first = int.from_bytes(body[1:2], "little", signed=True)
    offset = 2
    if first == 0:
        length = 0
    elif first > 4:
        length = first - 5
    elif 1 <= first <= 4 and len(body) >= offset + first:
        length = int.from_bytes(body[offset : offset + first], "little")
        offset += first
    else:
        raise ValueError("invalid Marshal string length")
    if len(body) < offset + length:
        raise ValueError("truncated Marshal string")
    return body[offset : offset + length].decode()


def _load(raw: bytes) -> object:
    if raw.startswith(b"\x04\x08"):
        return _marshal_string(raw)
    loaded: object = json.loads(raw)
    return loaded


def _expired(metadata: dict[str, object]) -> bool:
    expiry = metadata.get("exp")
    return isinstance(expiry, str) and clock.now() >= _time(expiry)


def unpack(raw: bytes, purpose: str | None = None) -> object:
    value = _load(raw)
    outer = as_dict(value)
    if outer is not None and "_rails" in outer:
        envelope = as_dict(outer["_rails"])
        if envelope is None:
            raise ValueError("invalid purpose")
        if (envelope.get("pur") or "") != (purpose or ""):
            raise ValueError("invalid purpose")
        if envelope.get("exp") is not None and _expired(envelope):
            raise ValueError("expired message")
        if "message" in envelope:
            return _load(decode64(envelope["message"]))
        return envelope.get("data")
    if purpose:
        raise ValueError("missing purpose")
    return value


def sign(
    value: object,
    salt: str,
    purpose: str | None = None,
    expiry: datetime | None = None,
    algorithm: str = "sha1",
    urlsafe: bool = False,
    padded: bool = True,
    *,
    plain_json: bool = False,
) -> str:
    metadata: dict[str, object] = {"data": value}
    if expiry:
        metadata["exp"] = _expiry(expiry)
    if purpose:
        metadata["pur"] = purpose
    wrapped: object = {"_rails": metadata} if purpose or expiry else value
    raw = (
        json.dumps(wrapped, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        if plain_json
        else encode(wrapped)
    )
    payload = (base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw)).decode()
    if not padded:
        payload = payload.rstrip("=")
    return payload + "--" + hmac.new(key(salt), payload.encode(), algorithm).hexdigest()


def verify(raw: str, salt: str, purpose: str | None = None, algorithm: str = "sha1") -> object:
    if "--" not in raw:
        raise ValueError("invalid message")
    payload, mac = raw.rsplit("--", 1)
    try:
        expected = hmac.new(key(salt), payload.encode(), algorithm).hexdigest()
        if not hmac.compare_digest(expected, mac):
            raise ValueError("invalid signature")
        return unpack(decode64(payload), purpose)
    except (TypeError, UnicodeError, KeyError) as error:
        raise ValueError("invalid message") from error


def cookie_envelope(name: str, value: object, expiry: datetime | None = None) -> bytes:
    return encode(
        {
            "_rails": {
                "message": b64(encode(value)),
                "exp": _expiry(expiry) if expiry else None,
                "pur": "cookie." + name,
            }
        }
    )


def sign_cookie(name: str, value: object, expiry: datetime | None = None) -> str:
    payload = b64(cookie_envelope(name, value, expiry))
    return payload + "--" + hmac.new(key("signed cookie"), payload.encode(), "sha1").hexdigest()


def _cookie_value(plaintext: bytes, name: str) -> object:
    # Cookie jars use NullSerializer: only the legacy message envelope is metadata.
    # Before Rails 5.2, raw JSON was accepted without a cookie-name purpose.
    if plaintext.startswith(b'{"_rails":{"message":"'):
        envelope = as_dict(json.loads(plaintext))
        fields = as_dict(envelope.get("_rails")) if envelope is not None else None
        if fields is None:
            raise ValueError("invalid cookie")
        if fields.get("pur") not in (None, "", "cookie." + name):
            raise ValueError("invalid cookie purpose")
        if fields.get("exp") and _expired(fields):
            raise ValueError("expired cookie")
        plaintext = decode64(fields.get("message"))
    # :json cookie jars explicitly reject historical Marshal values.
    loaded: object = json.loads(plaintext)
    return loaded


def verify_cookie(name: str, raw: str) -> object:
    text = unquote(raw)
    if "--" not in text:
        raise ValueError("invalid cookie")
    payload, mac = text.rsplit("--", 1)
    expected = hmac.new(key("signed cookie"), payload.encode(), "sha1").hexdigest()
    if not hmac.compare_digest(expected, mac):
        raise ValueError("invalid cookie signature")
    try:
        return _cookie_value(decode64(payload), name)
    except (TypeError, UnicodeError, KeyError) as error:
        raise ValueError("invalid cookie") from error


def encrypt_cookie(
    name: str, value: object, expiry: datetime | None = None, *, nonce: bytes | None = None
) -> str:
    iv = nonce if nonce is not None else secrets.token_bytes(12)
    encrypted = AESGCM(key("authenticated encrypted cookie", 32)).encrypt(
        iv, cookie_envelope(name, value, expiry), b""
    )
    return "--".join(map(b64, [encrypted[:-16], iv, encrypted[-16:]]))


def decrypt_cookie(name: str, raw: str) -> object:
    try:
        parts = unquote(raw).split("--")
        if len(parts) != 3:
            raise ValueError("invalid encrypted cookie")
        ciphertext, nonce, tag = (decode64(part) for part in parts)
        if len(nonce) != 12 or len(tag) != 16:
            raise ValueError("invalid encrypted cookie")
        plaintext = AESGCM(key("authenticated encrypted cookie", 32)).decrypt(
            nonce, ciphertext + tag, b""
        )
        return _cookie_value(plaintext, name)
    except (InvalidTag, TypeError, UnicodeError, KeyError) as error:
        raise ValueError("invalid encrypted cookie") from error


def _model_purpose(model: str, purpose: str) -> str:
    model = "Room" if model.startswith("Rooms::") else model
    underscored = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", model.replace("::", "/"))
    underscored = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", underscored).lower()
    return underscored + ("/" + purpose if purpose else "")


@lru_cache(maxsize=8192)
def _cached_signed_id(secret: str, model: str, id: int, purpose: str) -> str:
    return _signed_id(model, id, purpose, None)


def _signed_id(model: str, id: int, purpose: str, expiry: datetime | None) -> str:
    # ActiveStorage::Blob overrides both verifier and purpose for old installations.
    if model == "ActiveStorage::Blob":
        return sign(id, "ActiveStorage", purpose or "blob_id", expiry)
    return sign(
        id,
        "active_record/signed_id",
        _model_purpose(model, purpose),
        expiry,
        "sha256",
        True,
        False,
        plain_json=True,
    )


def signed_id(model: str, id: int, purpose: str = "", expiry: datetime | None = None) -> str:
    if expiry is None:
        # Deterministic without expiry; cached per installation secret.
        return _cached_signed_id(config.settings.secret_key_base, model, id, purpose)
    return _signed_id(model, id, purpose, expiry)


def verify_id(model: str, raw: str, purpose: str = "") -> int:
    if model == "ActiveStorage::Blob":
        value = verify(raw, "ActiveStorage", purpose or "blob_id")
    else:
        combined = _model_purpose(model, purpose)
        try:
            value = verify(raw, "active_record/signed_id", combined, "sha256")
        except ValueError:
            value = verify(raw, "active_record/signed_id", combined, "sha1")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("invalid model id")
    if isinstance(value, str) and not re.fullmatch(r"[+-]?\d+", value):
        raise ValueError("invalid model id")
    return int(value)


def stream_name(room_type: str, room_id: int) -> str:
    gid = f"gid://campfire/{room_type}/{room_id}".encode()
    return base64.urlsafe_b64encode(gid).decode().rstrip("=") + ":messages"


def user_rooms_stream(user_id: int) -> str:
    return b64(f"gid://campfire/User/{user_id}".encode()).rstrip("=") + ":rooms"


@lru_cache(maxsize=4096)
def _cached_stream(secret: str, name: str) -> str:
    return sign(name, "turbo/signed_stream_verifier_key", algorithm="sha256", plain_json=True)


def sign_stream(name: str) -> str:
    return _cached_stream(config.settings.secret_key_base, name)


def verify_stream(raw: str) -> str | int | float:
    value = verify(raw, "turbo/signed_stream_verifier_key", algorithm="sha256")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("invalid stream")
    return value


@lru_cache(maxsize=8192)
def _cached_sgid(secret: str, model: str, id: int) -> str:
    return sign(
        f"gid://campfire/{model}/{id}?expires_in", "signed_global_ids", "attachable", urlsafe=True
    )


def sgid(model: str, id: int) -> str:
    return _cached_sgid(config.settings.secret_key_base, model, id)


def verify_sgid(raw: str, purpose: str = "attachable") -> str:
    try:
        value = verify(raw, "signed_global_ids", purpose)
    except ValueError:
        fields = as_dict(verify(raw, "signed_global_ids"))
        if fields is None or fields.get("purpose") != purpose:
            raise ValueError("invalid GlobalID purpose") from None
        expires = fields.get("expires_at")
        if isinstance(expires, str) and expires and clock.now() > _time(expires):
            raise ValueError("expired GlobalID") from None
        value = fields.get("gid")
    if not isinstance(value, str):
        raise ValueError("invalid GlobalID")
    return value


def unverified_user_gid(raw: str | None) -> int | None:
    # reference/lib/rails_ext/action_text_attachables.rb explicitly permits User
    # mentions across secret rotation. This exception never authorizes blob access.
    if raw is None:
        return None
    try:
        envelope = as_dict(json.loads(decode64(raw.rsplit("--", 1)[0])))
        fields = as_dict(envelope.get("_rails")) if envelope is not None else None
        if fields is None:
            return None
        value = fields.get("data")
        if value is None and "message" in fields:
            # The Ruby extension extracts this URI without unmarshaling untrusted bytes.
            match = re.search(rb"gid://campfire/[^/]+/\d+", decode64(fields["message"]))
            value = match.group().decode() if match else None
        if not isinstance(value, str):
            return None
        if not value.startswith("gid://"):
            value = decode64(value).decode()
        uri = urlsplit(value)
        if uri.scheme == "gid" and uri.netloc and re.fullmatch(r"/User/\d+", uri.path):
            return int(uri.path.rsplit("/", 1)[1])
    except ValueError, KeyError, TypeError, UnicodeError:
        pass
    return None


def mask_csrf(raw: bytes) -> str:
    if len(raw) != 32:
        raise ValueError("invalid CSRF secret")
    pad = secrets.token_bytes(32)
    return (
        base64.urlsafe_b64encode(pad + bytes(a ^ b for a, b in zip(pad, raw, strict=True)))
        .decode()
        .rstrip("=")
    )


def valid_csrf(raw: bytes, token: str, path: str | None = None, method: str | None = None) -> bool:
    """Historical Rails token validation, retained for compatibility vectors."""
    try:
        if len(raw) != 32:
            return False
        value = decode64(token)
        if len(value) == 32:
            return hmac.compare_digest(value, raw)
        if len(value) == 64:
            value = bytes(a ^ b for a, b in zip(value[:32], value[32:], strict=True))
        if len(value) != 32:
            return False
        candidates = [raw, hmac.new(raw, b"!real_csrf_token", "sha256").digest()]
        if path is not None and method is not None:
            identifier = path.removesuffix("/") + "#" + method.lower()
            candidates.append(hmac.new(raw, identifier.encode(), "sha256").digest())
        return any(hmac.compare_digest(value, candidate) for candidate in candidates)
    except ValueError, TypeError:
        return False
