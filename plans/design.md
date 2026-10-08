# Design

Port of `once-campfire-django` to FastAPI/Starlette idioms. Behavior source of truth is the
Rails app pinned in `reference/` (`659f957`); the Django port supplies the native design.
Shared verification lives in `once-campfire-verification` (`docs/architecture.md` lists the
invariants every implementation keeps).

## Process and data flow

```
uvicorn (uvloop + httptools, N workers; N > 1 requires REDIS_URL)
  └─ ASGI stack (pure ASGI, no BaseHTTPMiddleware)
       compression  →  campfire middleware  →  FastAPI router
       (gzip q-values)  (epoch capture, ban check,      (async handlers)
                         sessions, bots, Fetch Metadata,
                         _method/suffix rewriting, form parsing,
                         cookies + security headers)
```

- Reads run on the event loop through a per-thread SQLite connection (WAL readers never
  wait for the writer). Write transactions run in one writer thread per process
  (`BEGIN IMMEDIATE`, 10 s busy timeout), so a writer waiting for SQLite's lock never stalls
  the loop. Post-commit hooks (Cable publications, job admission) run after `COMMIT`.
- No ORM: explicit SQL over the unmanaged Rails schema; Python's per-connection statement
  cache reuses prepared statements across requests. Rows map to typed dataclasses.
- Jinja2 renders the retained Turbo/Stimulus/Lexxy templates (compiled once, macros
  resolved at startup).

## Invariants kept (from the verification harness architecture inventory)

- Authenticated page cache (room, messages page, sidebar, search): the SQLite epoch is
  captured from a dedicated read-only observer (`PRAGMA data_version`) before the current
  user is loaded, checked at lookup and again before admission. Hits still authenticate,
  check session and room membership against current rows. Complete identity/gzip bodies
  are cached; cookies stay per request; flash state, bots and non-GET bypass it.
  `CAMPFIRE_RESPONSE_CACHE_MB` (default 64, per worker, 0 disables).
- Fetch Metadata write protection instead of CSRF tokens; bot routes and signed disk
  uploads keep their native exceptions.
- Bounded FTS probe (1,000 rows, newest IDs) with scoped fallback; exact-member direct
  room lookup.
- Message creation prepares pure sanitization before taking the write lock; membership,
  mentions and attachments resolve inside the transaction; message, rich text, FTS, room
  recency and unread markers commit together; one rendered fragment serves Cable and HTTP.
- Leased auxiliary SQLite job queue (`storage/db/jobs.sqlite3`): fenced leases, bounded
  retries, dead state, one queue transaction per message.
- Redis shares Cable publications (one pipeline per batch) and rate limits; required when
  more than one worker runs.

## Modules (each under ~800 lines)

| Module | Responsibility |
|---|---|
| `config`, `server`, `asgi` | Environment contract, Uvicorn launcher, ASGI stack assembly |
| `db`, `models`, `queries` | Connections, transactions, typed rows, shared reads |
| `rails` | Rails-compatible key derivation, signed/encrypted cookies, signed IDs, SGIDs |
| `richtext`, `richtext_render` | Sanitizer, Action Text plain text, presentation |
| `rendering`, `presentation`, `assets`, `urls` | Jinja environment, template view models, digested asset and signed paths |
| `response_cache`, `compression`, `middleware`, `request_context`, `context`, `http` | HTTP infrastructure |
| `routes/*` | FastAPI routers by area |
| `domain`, `messaging` | Transaction owners for rooms/users and for messages/boosts |
| `storage`, `files`, `media`, `vips` | Active Storage keys, uploads, ranged downloads, variants (libvips/ffmpeg/poppler) |
| `cable`, `bus` | Action Cable protocol over Starlette WebSockets, typed Redis relay |
| `jobs`, `push`, `opengraph` | Leased queue and webhooks, pinned Web Push, guarded unfurling |
| `cli`, `clock`, `jsonvalue` | Prepare/backup/restore, Rails time format, typed JSON narrowing |

## Verification steps

1. Port Django's native suite (`tests/test_application.py`, `tests/test_rails.py`) to
   pytest over real SQLite files; red first, then implement until green.
2. `make check`: ruff, pyright (strict), pytest.
3. Harness: `bin/browser` against a fresh container; `bin/benchmark --apps django,fastapi`.
4. Profile and transfer the fast implementations' mechanisms while keeping invariants.
