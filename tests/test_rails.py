"""Independent Rails outputs, including malformed tokens and old installations."""

import json
from collections.abc import Callable, Generator
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

import pytest

from campfire import config, rails

VECTORS = json.loads((Path(__file__).parent / "vectors/rails_compat.json").read_text())

type Example = dict[str, object]


def timestamp(value: object) -> datetime | None:
    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str) and value
        else None
    )


@pytest.fixture(autouse=True)
def vectors_secret() -> Generator[None]:
    original = config.settings.secret_key_base
    config.settings.secret_key_base = VECTORS["secret_key_base"]
    with patch("campfire.clock.now", return_value=timestamp(VECTORS["now"])):
        yield
    config.settings.secret_key_base = original


def check_verification(
    examples: list[Example], verify: Callable[[Example], object], expected: str = "expected"
) -> None:
    failures: list[object] = []
    for example in examples:
        with patch(
            "campfire.clock.now", return_value=timestamp(example.get("now", VECTORS["now"]))
        ):
            try:
                actual = verify(example)
            except ValueError:
                actual = None
        if actual != example[expected]:
            failures.append((example["case"], actual, example[expected]))
    assert failures == []


def text(example: Example, key: str) -> str:
    value = example[key]
    if value is None:
        return ""
    assert isinstance(value, str)
    return value


def test_key_generator() -> None:
    for example in VECTORS["key_generator"]:
        assert rails.key(example["salt"], example["length"]).hex() == example["key_hex"]


def test_signed_cookie_generation() -> None:
    for example in VECTORS["signed_cookies"]["generate"]:
        assert (
            rails.sign_cookie(example["name"], example["value"], timestamp(example["expires_at"]))
            == example["raw"]
        )


def test_signed_cookie_verification() -> None:
    check_verification(
        VECTORS["signed_cookies"]["verify"],
        lambda e: rails.verify_cookie(text(e, "name"), text(e, "raw")),
    )


def test_encrypted_cookie_generation() -> None:
    for example in VECTORS["encrypted_cookies"]["generate"]:
        nonce = rails.decode64(unquote(example["raw"]).split("--")[1])
        assert (
            rails.encrypt_cookie(
                example["name"], example["value"], timestamp(example["expires_at"]), nonce=nonce
            )
            == example["raw"]
        )


def test_encrypted_cookie_verification() -> None:
    check_verification(
        VECTORS["encrypted_cookies"]["verify"],
        lambda e: rails.decrypt_cookie(text(e, "name"), text(e, "raw")),
    )


def test_signed_id_generation() -> None:
    for example in VECTORS["signed_ids"]["generate"]:
        assert (
            rails.signed_id(
                example["model"],
                example["id"],
                example["purpose"],
                timestamp(example["expires_at"]),
            )
            == example["signed_id"]
        )


def test_signed_id_verification() -> None:
    examples: list[Example] = [
        dict(e, cast_expected=int(e["expected"]) if isinstance(e["expected"], (str, int)) else None)
        for e in VECTORS["signed_ids"]["verify"]
    ]
    check_verification(
        examples,
        lambda e: rails.verify_id(text(e, "model"), text(e, "signed_id"), text(e, "purpose")),
        expected="cast_expected",
    )


def test_sgid_generation() -> None:
    for example in VECTORS["sgids"]["generate"]:
        assert (
            rails.sign(
                example["data"],
                "signed_global_ids",
                example["purpose"],
                timestamp(example["expires_at"]),
                urlsafe=True,
            )
            == example["sgid"]
        )


def test_sgid_verification() -> None:
    check_verification(
        VECTORS["sgids"]["verify"], lambda e: rails.verify_sgid(text(e, "sgid"), text(e, "purpose"))
    )


def test_app_verifier_generation() -> None:
    for example in VECTORS["app_verifiers"]["generate"]:
        assert (
            rails.sign(
                json.loads(example["data_json"]),
                example["name"],
                example["purpose"],
                timestamp(example["expires_at"]),
            )
            == example["message"]
        )


def test_app_verifier_verification() -> None:
    examples: list[Example] = [
        dict(e, expected=json.loads(e["expected_json"]) if e["expected_json"] is not None else None)
        for e in VECTORS["app_verifiers"]["verify"]
    ]
    check_verification(
        examples,
        lambda e: rails.verify(
            text(e, "message"),
            text(e, "name"),
            e["purpose"] if isinstance(e["purpose"], str) else None,
        ),
    )


def test_turbo_stream_generation() -> None:
    for example in VECTORS["turbo_stream_names"]["generate"]:
        assert rails.sign_stream(example["stream_name"]) == example["signed"]


def test_turbo_stream_verification() -> None:
    check_verification(
        VECTORS["turbo_stream_names"]["verify"], lambda e: rails.verify_stream(text(e, "signed"))
    )


def test_csrf_validity() -> None:
    raw = rails.decode64(VECTORS["csrf"]["session_token"])
    failures = [
        example["case"]
        for example in VECTORS["csrf"]["validity"]
        if rails.valid_csrf(raw, example["token"], example["path"], example["method"])
        != example["expected"]
    ]
    assert failures == []


def test_session_continuity() -> None:
    example = VECTORS["session"]
    assert (
        rails.decrypt_cookie("_campfire_session", example["session_cookie_raw"])
        == example["session"]
    )
    assert (
        rails.verify_cookie("session_token", example["session_token_raw"])
        == example["session_token_value"]
    )


def test_user_only_legacy_mention_exception() -> None:
    for example in VECTORS["unverified_sgids"]:
        expected = example["expected"]
        expected_id = (
            int(expected.rsplit("/", 1)[1])
            if isinstance(expected, str) and "/User/" in expected
            else None
        )
        # This helper extracts identity; the richtext caller performs the lookup.
        if example["case"] == "missing user":
            expected_id = 999
        assert rails.unverified_user_gid(example["sgid"]) == expected_id, example["case"]
    assert rails.unverified_user_gid(rails.sgid("ActiveStorage::Blob", 1)) is None


def test_invalid_encryption_is_value_error() -> None:
    token = rails.encrypt_cookie("_campfire_session", {"_csrf_token": "old tab"})
    corrupted = ("A" if token[0] != "A" else "B") + token[1:]
    with pytest.raises(ValueError):
        rails.decrypt_cookie("_campfire_session", corrupted)
    for token in ["garbage", "!!!", "a--b--c"]:
        with pytest.raises(ValueError):
            rails.decrypt_cookie("_campfire_session", token)


def test_secret_rotation_does_not_reuse_cached_key() -> None:
    token = rails.sign_cookie("session_token", "existing-session")
    config.settings.secret_key_base = VECTORS["rotated_secret_key_base"]
    with pytest.raises(ValueError):
        rails.verify_cookie("session_token", token)


def test_active_support_json_line_separators() -> None:
    # load_defaults 8.1 leaves JS separators literal but escapes HTML entities.
    assert rails.encode("<>&\u2028\u2029") == '"\\u003c\\u003e\\u0026\u2028\u2029"'.encode()
