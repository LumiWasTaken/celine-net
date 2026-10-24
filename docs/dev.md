# Development

## Run

```sh
./start.sh                                # the app on :8766
./start.sh --port 8900                    # another port (the same as CELINE_PORT=8900)
CELINE_DATA_DIR=/path/to/data ./start.sh  # another data dir
```

Use a scratch data dir, never real chats. Stop the server through a pid file
(`setsid sh -c 'echo $$ > server.pid; exec ./start.sh ...'`, then `kill -- -$(cat server.pid)`).
Set the provider in Settings, Models (any OpenAI-compatible server works for the setup).

## Boxes

The first start writes `var/keys/` (`server/celine/keys.py`): the stack's keys, the boxes' hash files, the connectors' clients file and SearXNG's settings; `celine keys [dir]` does the same by hand.
`docker-compose.dev.yml` runs its shellbox, browser and SearXNG in containers on those files and publishes them on loopback (8770, 8771 and 5900, 8888):
`docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d shellbox browser searxng`, then `./start.sh`.
While no address is saved the app looks at those ports (`settings/services.py` DEFAULTS, or `CELINE_SHELL_URL`, `CELINE_BROWSER_URL` and `CELINE_SEARXNG_URL` when set) and Setup offers each box it finds with that address filled in; once saved in Settings, Features, the saved one wins.
The shellbox calls the app's model proxy on `CELINE_PORT` (default 8766): export it in the shell that runs the compose when the app runs on another port (`./start.sh --port N` is the same as `CELINE_PORT=N`).

## Dev switches

- `--dev`: the API for the Vite dev server (`./start.sh dev`).
- `CELINE_CONFIG_DIR`, `CELINE_DATA_DIR`: another config or data dir.
- `CELINE_STATIC_DIR`: serve a web build from elsewhere.
- `WEB_CONCURRENCY`: must be 1; there is one writer.
- `celine purge --under N`: delete and ignore every chat under N messages (`--dry-run` counts).

## Gates

Every push passes all of these; the exit code is the check. GitHub runs them on every push and pull request (`.github/workflows/test.yml`).

```sh
uv run pytest -q                     # the app and the Discord connector, on fakes, in parallel
uv run ruff check                    # pyflakes and a few pycodestyle checks (pyproject.toml)
cd web && npm ci && npm run build    # prebuild: useReveal check, eslint (hooks); then tsc and vite
cd wa-link && go test ./...          # only when wa-link changed
```

- `tests/test_imports.py` keeps the package boundaries (`.importlinter`; no new two-way imports).
- A flaky test is a failing test. Run the suite twice.

Opt-in markers: `-m live` (a real model server, `CELINE_LIVE=http://host:11434/v1`), `-m shellbox`, `-m browser`.

## Tests

1. Fixed clocks: pass `now` in, or take the `clock` fixture (`tests/conftest.py`) and monkeypatch it over the `now` of the modules under test (`monkeypatch.setattr(deliver, "now", clock.iso)`), then `clock.advance(s)`. A test passes at any time of day.
2. No sleeps: `await until(check)` (`until_sync(check)` without a loop) from `tests/chat_fixtures.py`, an `anyio.Event` the fake sets, or `anyio.wait_all_tasks_blocked()`. A fake that hangs uses `anyio.sleep_forever()`.
3. No schedule, timer or trigger comes due by the real clock.
4. Small fixtures only (`tests/*_fixtures.py`, `tests/data/`), `tmp_path` for files, nothing shared between tests (xdist).
5. The test model (`tests/fake_backend.py`) replays a script of backend events.
6. One test per behaviour, named as a sentence. A bug fix comes with the test that fails without it.

## Migrations

Stored data changes one way: a numbered SQL migration, applied once, with the code for the old shape removed in the same change.

1. Files are `server/celine/db/migrations/NNNN_lower_snake.sql`. `0001_schema.sql` is the whole schema; later files run on top, in order, each in one transaction. One numbered chain per SQLite file; a file whose version is older than the first file of its chain is refused.
2. A database newer than the build stops the server.
3. Convert existing rows in the migration, never on read. A conversion SQL cannot do (files, settings TOML) is a one-time Python step that is deleted once everyone has run it.
4. Never edit a migration that is released; fix forward.
5. `tests/test_migrate.py` holds `LATEST`: set it to your number.
6. Every other SQLite file has its own folder and the same runner (`db/migrate.py`, `migrate(path, dir)`, the version in the file's `PRAGMA user_version`): `db/migrations/log` (`var/log.db`, the append-only tables), `db/migrations/notebook` (`var/notebook/.index.db`, the notes' index; both attached to celine.db by the Store), `auth/migrations`, `kb/migrations/chat`, `kb/migrations/documents`. `0001` is the whole schema. A file with tables and no version, or a newer one, is refused. `migrate_all(db_path)` brings celine.db, log.db and the index up together; the app calls it at start.

## Worktrees

Work on a branch in its own worktree: `git worktree add ../x -b my-branch`, `git fetch -q origin` first. Commit small with short lowercase messages.
