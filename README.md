# once-campfire-fastapi

A native FastAPI implementation of [ONCE Campfire](https://github.com/basecamp/once-campfire).
Explicit SQL maps the existing Rails SQLite schema, Jinja templates retain the original
Turbo/Stimulus/Lexxy interface, Uvicorn (uvloop/httptools) serves HTTP and Action Cable
WebSockets, and libvips/ffmpeg/poppler process media. No Ruby or another Campfire
implementation runs in the application process.

The original schema, files, bcrypt passwords and Rails sessions remain compatible.
See [design](plans/design.md) and [contracts](plans/contracts.md).

```sh
git submodule update --init
make install assets
export SECRET_KEY_BASE="$(openssl rand -hex 64)"
HTTP_PORT=8080 make serve
```

Storage defaults to `storage/db/production.sqlite3` and `storage/files`. Keep the existing
`SECRET_KEY_BASE` to retain signed/encrypted cookies. Set `CAMPFIRE_STORAGE_PATH` to relocate
both. `python -m campfire.cli backup` snapshots the application and job queue;
`python -m campfire.cli restore` restores snapshots while the application is stopped.

A single process works without Redis. With `REDIS_URL`, HTTP workers default to the assigned
CPU count, capped at four; `WEB_WORKERS` overrides it. Multiple workers require Redis for
shared Cable publications and rate limits. Each worker has a 64 MiB page-cache budget;
`CAMPFIRE_RESPONSE_CACHE_MB=0` disables it. Authentication and room access stay fresh, and
every SQLite commit invalidates complete cached HTML and gzip bodies. Jobs use a leased
SQLite queue that survives restarts. Put TLS termination in front of the application and
configure `TRUSTED_PROXIES` to that proxy's address.

Production image: `make image` builds `once-campfire-fastapi:app`, labelled with
`org.opencontainers.image.revision`. Its container contract (environment, `/rails/storage`
mounts, port) matches the Django port so the shared verification harness runs it unchanged.

Run the checks with `make check` (ruff, pyright strict, pytest against real SQLite files).

## Known differences

- Shared rooms preserve the first unread message until the member reads it, matching the
  updated Rails and Laravel implementations. Direct pings retain their latest-message marker.
- Browser writes use Fetch Metadata instead of CSRF tokens. Unsafe requests reject a mismatched
  or null Origin and require `Sec-Fetch-Site: same-origin` or `same-site`; missing metadata is
  accepted only over plain HTTP without `FORCE_SSL=1` or `true`. GET/HEAD and authenticated bot
  routes retain their existing behavior. Forms and uploads generate no CSRF tokens; old signed
  cookies and token-bearing tabs continue to work. Signed disk uploads require the
  authenticated owner and the expiring upload capability, independently of Fetch Metadata.
- Authenticated read pages use a bounded, commit-versioned cache after fresh authorization;
  complete HTML and gzip representations are reused while cookies remain per request.
- Sidebar connection refresh waits for the current Turbo frame to finish loading, preventing
  an aborted response on startup or reconnect.
- Cached message copy-link buttons store paths and resolve them against the current page.
- Search selects the newest 100 matching messages by insertion ID, then displays them in ID
  order. Backdated messages can appear in a different order from the original Rails app.
- TLS terminates at a configured proxy.
- Attached message downloads recheck room membership; native draft uploads belong to their
  uploader. Existing Rails unattached signed draft URLs remain usable after sign-in.
- Direct-ping autocomplete explicitly requests JSON, repairing the original fetch-header bug.
- HTML whitespace, malformed HTML repair, cache validators and native-library media bytes can
  differ; canonical editor plain text and actual browser workflows are tested.
- App and queue snapshots are separate atomic SQLite backups; job delivery is at least once.
  A webhook whose connection fails is retried (bounded, then marked dead) where Rails fails
  the job once; timeouts post the Rails "Failed to respond within 7 seconds" reply instead.
- Not a difference, recorded as a decision: bot webhook URLs may point at private, loopback
  or LAN addresses, exactly as in Rails (`app/models/webhook.rb` and `SECURITY.md`,
  "Intentional behavior"). Only an administrator sets the URL, and self-hosted bots
  commonly run on internal hosts. Redirects are not followed. Link unfurling and Web Push,
  which any member or browser can point anywhere, keep the private-network guard and
  connect to the validated, pinned address.

## Attribution

MIT (see `MIT-LICENSE`, copyright 37signals). This port follows the design of
[once-campfire-django](https://github.com/basecamp/once-campfire-django): its Jinja
templates, frontend overrides, asset build algorithm, sounds/translation data, Rails
compatibility vectors and test suite were adapted here. Templates and asset-generation
algorithms originally came from the Go port; media and Rails compatibility contracts from
the original application and existing ports. Vendored framework assets under
`assets/sources/vendor/` retain their own MIT licenses. The immutable original reference is
pinned to `659f95748a115a360a37db9bf80a5361a560e14f`.
