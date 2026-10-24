# AGENT.md

The map of the code and the rules it follows.

## Where things live

| Path | What |
|---|---|
| `server/celine/` | the app: one Python process (FastAPI), one SQLite writer |
| `web/src/` | the UI: React, TypeScript, Vite |
| `web/src/ui/` | the kit: the only code that draws buttons, heads, menus, dialogs, sheets, toasts, fields ([docs/ui/components.md](docs/ui/components.md)) |
| `tests/` | pytest on fakes; `*_fixtures.py` build data, `data/` holds small files; `-m live` needs a model server |
| `config/` | shipped config: `celine.toml`, `llm.toml`, `kb.toml`, `skills.toml`, `mcp.toml`, `context.toml`, `her/` (persona, talk, voice), `guard/` (the guard's persona, talk and judge prompts), `notebook_seed/` (first notes, copied once), `image_presets/`, `music_presets/`, `schedules/`, `searxng/` |
| `connectors/discord/` | discord-link, the Discord connector (Python) |
| `wa-link/` | the WhatsApp connector (Go) |
| `shellbox/`, `shellbox-manager/` | the agent's Linux container (shellboxd, the coding agent under `agents/`) and its reset |
| `browser/` | the agent's browser (Brave, browserd) |
| `docs/` | `ARCHITECTURE.md`, `DEPLOY.md`, `dev.md`, `customize.md`, `ui/components.md`, `ui/words.md` |

### server/celine

| Module | What |
|---|---|
| `__main__.py`, `config.py`, `boot.py`, `reload.py` | preflight and lifespan; celine.toml into a Config and the readers' helpers; the whole load (every config file, var/settings, its row), which writes nothing; config reloaded in place |
| `core/` | the single writer and event append, EventBus, ids and times, Services |
| `db/` | `migrations/0001_schema.sql` and the chains for `log/` and `notebook/`; `migrate.py` applies them |
| `conversations/` | conversation rows, the message tree, snapshot and replay, routes |
| `turns/` | one turn: dispatcher, runner, tool chokepoint, context, compaction, titles; `reply.py` the one place that touches a stored reply (citation tokens only) |
| `backends/` | model APIs (openai, anthropic), router, queue, the test model is `tests/fake_backend.py` |
| `skills/` | the tool registry and the tools without a module of their own; `guides/<skill>.md` the skill guides; `recall.py` what comes back on its own |
| `kb/` | knowledge bases: the chat archive, documents, instant and semantic search, the connectors' feed |
| `settings/` | `var/settings` and `var/secrets`: shapes, validation, `registry.py` (every setting), `features.py` (the feature catalog and probes), `services.py` (box addresses) |
| `her.py`, `prompts.py` | the agent's row: name, pronouns, persona, talk, voice, portrait; `text(__file__, name)` reads a `.md` beside the module that sends it |
| `missions/`, `sub_agents/` | missions and sub-agents |
| `plans/` | the plan, one per conversation |
| `schedules/`, `triggers/` | routines: schedules and their runs (one try, no retries); watch and event triggers |
| `intercom/` | messages between everyone: rows, delivery, threads |
| `guard/` | the guard: one switch, one module per concern, prompts in `config/guard/` |
| `bugreports/` | bug reports the agent files, each an intercom thread |
| `homes.py` | where a conversation lives and the link that opens it; the client never works it out |
| `jobs/` | steps that outlive their turn |
| `work.py`, `hold.py`, `leftovers.py` | the queue of background model jobs; why no turn may start now; what a done mission leaves open |
| `events.py`, `texts.py`, `approvals.py`, `metrics.py` | the events list; every long text's history; approvals before something runs; Prometheus numbers |
| `attachments.py`, `box_files.py` | files on a message; files between the app and the box |
| `comfyui/`, `image_catalog.py`, `vision.py` | images and music; the model catalog; one question about one image |
| `terminals/`, `browser/`, `vault/`, `artifacts/`, `shares.py` | terminals, browser, vault (bw), pages, and shares of its box at /share/<token>/ |
| `bridge/`, `mcp.py` | the bridged chat (WhatsApp); MCP servers as skills |
| `auth/` | login: passkey, password, PIN |
| `api/` | HTTP routes and the WebSocket |
| `clock.py`, `errors.py`, `logs.py`, `ops.py`, `keys.py` | time zone; errors; logging; `celine doctor/export/purge/keys` |

### web/src

| Path | What |
|---|---|
| `App.tsx`, `main.tsx` | routes, the rail, the socket, the global overlays |
| `ui/` | the kit; `ui/index.ts` exports it; `ui/ui.css` styles it |
| `views/` | one place each: `Celine`, `Missions`, `Routines`, `Notebook`, `Documents`, `Chats`, `Intercom`, `Terminals`, `Terminal`, `Browser`, `Search`, `Artifact`, `Welcome`, `settings/` |
| `components/` | app parts made of kit parts: Transcript, Composer, ToolRow, ConversationList, ConversationPanel, Markdown, Plan, ProviderFields |
| `lib/` | api clients, socket and bus, `nav.tsx`, `prefs.ts` (this device's conveniences in localStorage), hooks (`useResource`, `useAction`, `useReveal`, `useConversation`) |
| `styles/` | `tokens.css` (every value), `base.css` (keyframes, resets) |

## Words

| Word | Means |
|---|---|
| her, she | the agent (`her.py` holds its row; the pronoun is a setting, the code keeps "her") |
| the box | shellbox, the agent's Linux container |
| the house | the app itself and what runs inside it |
| outside | text from anyone but the user: web pages, mail, chats; untrusted |
| lands | a row or event arrives where it belongs |
| Bastet | the guard (`guard/`) |
| run, mission, job | a routine's one try; a goal she works on with sub-agents; a step that outlives its turn |

## Gates

```sh
uv run pytest -q
cd web && npm ci && npm run build     # prebuild: useReveal check; then tsc and vite
cd wa-link && go test ./...           # when wa-link changed
```

An instance on a scratch data dir: `CELINE_DATA_DIR=DIR/data ./start.sh --port 8801`.

## Rules

1. No backwards compatibility: no shims, aliases or old shapes. Stored data changes once, in a migration, and the old code goes in the same change.
2. An interactive turn that fails fails with its reason. Nothing retries, rewrites or re-asks behind the user's back; routines run once and wait for their model.
3. The server owns state: lists, counts, labels, paths and effective settings come from the server through explicit events. The client never guesses.
4. Routes are plain REST: `/api/<plural-noun>[/{id}[/<sub-resource>]]`, kebab-case, a verb only as a trailing action (`/run`, `/stop`, `/test`), singular only for a singleton (`/api/agent`, `/api/status`), sockets under `/ws/`.
5. A text a model reads lives beside the code that sends it (`prompts.text`), or in `config/` when a user is meant to edit it.
6. A feature is one entry in `settings/features.py`; a service address is a setting with a probe; nothing needs a restart.
7. UI: draw with the kit in `web/src/ui`; words from `docs/ui/words.md` or an icon with a tooltip; no explainer text; every colour, size, radius, shadow and duration from `web/src/styles/tokens.css`; one design.
8. The page never scrolls: a screen fits the viewport and only inner regions scroll (a transcript, a list, a text box, a card body).
9. No em dash, en dash or " · " in any text a user or a model sees. Code that needs one writes the escape.
10. Tests use fixed clocks and wait on conditions, never on sleeps.
11. Small commits with short lowercase messages. Every gate green before a push.
