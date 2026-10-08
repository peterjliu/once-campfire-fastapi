"""Environment contract shared with the other Campfire ports."""

import os
from dataclasses import dataclass, replace
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _storage_path() -> Path:
    return Path(os.environ.get("CAMPFIRE_STORAGE_PATH", BASE_DIR / "storage"))


@dataclass
class Settings:
    secret_key_base: str
    storage_path: Path
    files_path: Path
    database_path: Path
    force_ssl: bool
    response_cache_bytes: int
    redis_url: str
    vapid_public_key: str
    vapid_private_key: str
    vapid_subject: str
    start_jobs: bool = True
    # Rails' has_secure_password cost; tests lower it to bcrypt's minimum.
    bcrypt_rounds: int = 12

    def snapshot(self) -> Settings:
        return replace(self)

    def restore(self, other: Settings) -> None:
        self.__dict__.update(other.__dict__)


def load() -> Settings:
    secret = os.environ.get("SECRET_KEY_BASE", "")
    if not secret:
        raise RuntimeError("SECRET_KEY_BASE is required")
    storage = _storage_path()
    return Settings(
        secret_key_base=secret,
        storage_path=storage,
        files_path=Path(os.environ.get("CAMPFIRE_FILES_PATH", storage / "files")),
        database_path=Path(
            os.environ.get("CAMPFIRE_DATABASE_PATH", storage / "db" / "production.sqlite3")
        ),
        force_ssl=os.environ.get("FORCE_SSL", "").lower() in ("1", "true"),
        response_cache_bytes=max(0, int(os.environ.get("CAMPFIRE_RESPONSE_CACHE_MB", "64")))
        * 1024
        * 1024,
        redis_url=os.environ.get("REDIS_URL", ""),
        vapid_public_key=os.environ.get("VAPID_PUBLIC_KEY", ""),
        vapid_private_key=os.environ.get("VAPID_PRIVATE_KEY", ""),
        vapid_subject=os.environ.get("VAPID_SUBJECT", "mailto:campfire@example.com"),
    )


settings = load()
GENERATED = BASE_DIR / "assets/generated"
REFERENCE = BASE_DIR / "reference"
TEMPLATES = Path(__file__).resolve().parent / "templates"
