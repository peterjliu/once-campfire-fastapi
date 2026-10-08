"""SQLite connections and transactions over the unmanaged Rails schema.

Each thread owns one connection (Python's statement cache then reuses prepared
statements across requests). Reads run wherever the caller is, normally the event loop.
Write transactions use `BEGIN IMMEDIATE`; async handlers send them to the single writer
thread with `awrite`, so a writer waiting for SQLite's lock never stalls the loop.
"""

import asyncio
import functools
import sqlite3
import threading
from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import config

type Param = str | int | float | bytes | None
type Params = Sequence[Param]


class _Local(threading.local):
    connection: sqlite3.Connection | None = None
    generation: int = -1
    transaction: Transaction | None = None


_local = _Local()
_generation = 0
_tracers: list[Callable[[str], None]] = []
_connections: list[sqlite3.Connection] = []
_registry_lock = threading.Lock()
WRITER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="campfire-writer")


def open_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path,
        timeout=10,
        isolation_level=None,
        check_same_thread=False,
        cached_statements=512,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _trace(statement: str) -> None:
    for tracer in list(_tracers):
        tracer(statement)


def connection() -> sqlite3.Connection:
    current = _local.connection
    if current is not None and _local.generation == _generation:
        return current
    if current is not None:
        current.close()
    current = open_connection(config.settings.database_path)
    if _tracers:
        current.set_trace_callback(_trace)
    with _registry_lock:
        _connections.append(current)
    _local.connection = current
    _local.generation = _generation
    return current


def reset() -> None:
    """Invalidate every thread's connection (each reopens lazily on next use)."""
    global _generation
    _generation += 1
    current = _local.connection
    if current is not None:
        current.close()
        _local.connection = None
    _local.transaction = None


@contextmanager
def trace(callback: Callable[[str], None]) -> Generator[None]:
    """Record every executed statement on every connection (tests and diagnostics)."""
    _tracers.append(callback)
    with _registry_lock:
        for existing in _connections:
            try:
                existing.set_trace_callback(_trace)
            except sqlite3.ProgrammingError:
                continue
    try:
        yield
    finally:
        _tracers.remove(callback)


def one(sql: str, params: Params = ()) -> sqlite3.Row | None:
    cursor = connection().execute(sql, params)
    row: sqlite3.Row | None = cursor.fetchone()
    cursor.close()
    return row


def rows(sql: str, params: Params = ()) -> list[sqlite3.Row]:
    result: list[sqlite3.Row] = connection().execute(sql, params).fetchall()
    return result


def value(sql: str, params: Params = ()) -> object:
    row = one(sql, params)
    return None if row is None else row[0]


def integer(sql: str, params: Params = ()) -> int | None:
    found = value(sql, params)
    return found if isinstance(found, int) else None


def exists(sql: str, params: Params = ()) -> bool:
    return one(sql, params) is not None


def run(sql: str, params: Params = ()) -> int:
    """Execute a write; returns the last inserted row ID."""
    cursor = connection().execute(sql, params)
    rowid = cursor.lastrowid or 0
    cursor.close()
    return rowid


def run_many(sql: str, params: Sequence[Params]) -> None:
    connection().executemany(sql, params)


def placeholders(count: int) -> str:
    return ",".join("?" * count)


@dataclass
class Transaction:
    connection: sqlite3.Connection
    callbacks: list[Callable[[], object]] = field(default_factory=list[Callable[[], object]])
    files: list[Path] = field(default_factory=list[Path])

    def on_commit(self, callback: Callable[[], object]) -> None:
        self.callbacks.append(callback)

    def track_file(self, path: Path) -> None:
        self.files.append(path)


def begin(target: sqlite3.Connection) -> None:
    target.execute("BEGIN IMMEDIATE")


def current() -> Transaction | None:
    return _local.transaction


@contextmanager
def transaction() -> Generator[Transaction]:
    """Outermost call owns BEGIN/COMMIT; nested calls join it."""
    existing = _local.transaction
    if existing is not None:
        yield existing
        return
    target = connection()
    begin(target)
    tx = Transaction(target)
    _local.transaction = tx
    try:
        yield tx
        target.execute("COMMIT")
    except BaseException:
        if target.in_transaction:
            target.execute("ROLLBACK")
        for path in tx.files:
            path.unlink(missing_ok=True)
        raise
    finally:
        _local.transaction = None
    for callback in tx.callbacks:
        callback()


def write[T](function: Callable[[], T]) -> T:
    with transaction():
        return function()


async def awrite[**P, T](function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run a write (which opens its own transaction) on the writer thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(WRITER, functools.partial(function, *args, **kwargs))
