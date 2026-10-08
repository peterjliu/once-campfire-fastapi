"""Operator commands: prepare the schema, back up and restore snapshots."""

import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from . import config

MIGRATIONS = config.REFERENCE / "db/migrate"
SCHEMA = Path(__file__).with_name("schema.sql")


class PrepareError(RuntimeError):
    """The existing database needs the pinned Rails migrations first."""


def required_versions() -> list[str]:
    return sorted(path.stem.split("_", 1)[0] for path in MIGRATIONS.glob("*.rb"))


def prepare() -> None:
    """Initialize an empty Rails-compatible schema; reject unsupported upgrade state."""
    settings = config.settings
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.files_path.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(settings.database_path)) as connection:
        initialized = connection.execute(
            "SELECT name FROM sqlite_master WHERE name='accounts'"
        ).fetchone()
        if initialized is None:
            connection.executescript(SCHEMA.read_text())
            connection.executemany(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                [(version,) for version in required_versions()],
            )
            connection.commit()
        else:
            present = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }
            missing = sorted(set(required_versions()) - present)
            if missing:
                raise PrepareError(
                    "Run the pinned Rails database migrations before upgrade: " + ", ".join(missing)
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS index_messages_on_room_id_and_updated_at "
            "ON messages(room_id,updated_at)"
        )
        connection.commit()
        connection.execute("PRAGMA journal_mode=WAL")


def _snapshot(source: Path, target: Path) -> None:
    pending = target.with_suffix(".pending")
    with closing(sqlite3.connect(source)) as origin, closing(sqlite3.connect(pending)) as copy:
        origin.backup(copy)
    os.replace(pending, target)


def backup(output: Path | None = None) -> Path:
    """Atomic SQLite snapshots of the application and job queue without stopping writers."""
    settings = config.settings
    target = output or settings.storage_path / "backups/production.sqlite3"
    target.parent.mkdir(parents=True, exist_ok=True)
    _snapshot(settings.database_path, target)
    queue = settings.storage_path / "db/jobs.sqlite3"
    if queue.is_file():
        _snapshot(queue, target.with_name("jobs.sqlite3"))
    return target


def restore() -> None:
    """Restore checked snapshots while the application is stopped."""
    from . import db, jobs

    settings = config.settings
    snapshots = settings.storage_path / "backups"
    jobs.reset()
    db.reset()
    targets = {
        "production.sqlite3": settings.database_path,
        "jobs.sqlite3": settings.storage_path / "db/jobs.sqlite3",
    }
    for filename, target in targets.items():
        source = snapshots / filename
        if not source.is_file():
            if filename == "production.sqlite3":
                raise PrepareError("Application snapshot is missing")
            continue
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise PrepareError(f"Invalid snapshot: {filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".restoring")
        shutil.copyfile(source, temporary)
        for suffix in ("-wal", "-shm"):
            target.with_name(target.name + suffix).unlink(missing_ok=True)
        os.replace(temporary, target)
    db.reset()


def main(arguments: list[str]) -> int:
    commands = {"prepare": prepare, "backup": backup, "restore": restore}
    if len(arguments) != 1 or arguments[0] not in commands:
        print("usage: python -m campfire.cli prepare|backup|restore", file=sys.stderr)
        return 2
    try:
        commands[arguments[0]]()
    except PrepareError as error:
        print(error, file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
