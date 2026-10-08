# Contracts and evidence

What has been verified, how, and what remains open. Update this with actual results; do
not record anything that was not run.

## Verified (2026-10-08, host `twenty`, rootless podman)

| Gate | Command | Result |
|---|---|---|
| Lint | `make lint` (ruff check + ruff format --check) | clean |
| Types | `make typecheck` (pyright strict) | 0 errors, 0 warnings |
| Native suite | `make test` (real SQLite files under `tmp/`) | 114 passed, 0 skipped |
| Browser flows | `bin/browser --base ...` against a fresh `once-campfire-fastapi:app` container with empty storage, two separate containers | PASS both runs (setup, two-tab live messaging, duplicate suppression, stored-markup safety, permalinks, editing, search, profile/account, QR codes, live room create/rename, bots, custom styles, session transfers, joining, autocomplete direct pings) |
| Frontend regressions | harness `test/sidebar-reload.mjs`, `test/editor-preservation.mjs` on `assets/overrides/controllers/rooms_list_controller.js`; `test/autocomplete-json.mjs` on `assets/overrides/lib/autocomplete/base_autocomplete_handler.js` | 3/3 passed |
| Route contracts | `bin/benchmark --apps fastapi --workspace ~/github --preflight` on the seeded Rails fixtures | room_show, messages_page, sidebar, search, avatar, static_css, up passed in 3 rounds; post write audit verified |

The harness is `once-campfire-verification` with the optional FastAPI adapter
(`fastapi-adapter` branch, `0c84384`), fixtures seeded by the pinned Rails reference image.

## Decisions

- Bot webhooks are not restricted to public addresses, matching Rails
  (`reference/app/models/webhook.rb`, `reference/SECURITY.md` "Intentional behavior").
  Pinned by `tests/test_jobs.py::test_webhook_may_target_private_network_bots_like_rails`.
  Redirects are not followed; unreachable endpoints fail the job (bounded retries).

## Not yet measured

- Throughput against Django and Rails on the same host (`bin/benchmark --apps
  django,fastapi`); no performance claim is made until that run is recorded.
- Multi-worker Redis topology under the browser flow (the browser gate runs one worker).
