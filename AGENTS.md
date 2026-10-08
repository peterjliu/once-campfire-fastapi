# once-campfire-fastapi

Native FastAPI Campfire. Read the pinned public Rails `reference/` for existing behavior;
never change its checkout. Runtime must remain self-contained Python/FastAPI. The Django
port (`once-campfire-django`) is the design this repository follows; see `plans/design.md`.

- Preserve the existing SQLite schema, storage keys, bcrypt passwords and Rails cookies.
  The schema is unmanaged: SQL targets the Rails tables directly; no ORM or framework
  session tables.
- Reads run on the event loop; writes run in the per-process writer thread through
  `campfire.db.write` / `campfire.db.awrite`. Post-commit work goes through
  `Transaction.on_commit`.
- Native jobs use `storage/db/jobs.sqlite3` with leased, fenced, bounded retries. Redis
  shares Cable publications and rate limits; it is required when `WEB_WORKERS` exceeds one.
- Keep the authenticated page-cache invariants in `plans/design.md` (epoch captured before
  authentication, checked at lookup and admission; fresh authorization on every hit).
- Keep deliberately changed behavior in README's "Known differences" and update
  `plans/contracts.md` with actual evidence and outstanding limits.
- Frontend changes belong in `assets/overrides/`; run `make assets` afterward.
  Vendored framework asset distributions retain their licenses and upstream formatting.
- Python: `uv` with pinned versions (`pyproject.toml` + `uv.lock`), full type annotations,
  no `Any`, no `getattr`; pyright strict and ruff must be clean. Keep modules under ~800
  lines. Use the Makefile; no shell scripts besides the container entrypoint.
- Verify with `make check` (ruff, pyright, pytest). Tests create isolated real SQLite
  databases under ignored `tmp/`; no seeds are skipped.
- Build production with `make image` (tags `once-campfire-fastapi:app` with the
  `org.opencontainers.image.revision` label). `HTTP_PORT` configures the listener,
  `CAMPFIRE_STORAGE_PATH` the mounted storage root, `SECRET_KEY_BASE` the retained
  installation key, `TRUSTED_PROXIES` the TLS proxy IP, `WEB_WORKERS`/`REDIS_URL` the
  worker topology and `CAMPFIRE_RESPONSE_CACHE_MB` the per-worker page cache.
- Back up with `python -m campfire.cli backup`; restore with `python -m campfire.cli restore`
  while the app is stopped. Application and queue backups are separate atomic captures;
  external job side effects have at-least-once delivery.
- Keep raw benchmark/test results, scratch generators and runtime storage ignored.
