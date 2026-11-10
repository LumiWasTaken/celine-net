# AGENT.md

The map of the code and the rules it follows.

## Where things live

| Path | What |
|---|---|
| `server/celine/` | the app: one Python process (FastAPI), one SQLite writer |
| `web/src/` | the UI: React, TypeScript, Vite |
| `web/src/ui/` | the kit: the only code that draws buttons, heads, menus, dialogs, sheets, toasts, fields ([docs/ui/components.md](docs/ui/components.md)) |
| `tests/` | pytest on fakes; `*_fixtures.py` build data, `data/` holds small files; `-m live` needs a model server |
| `tools/testdata/` | `make.py` builds a data dir of made-up data, `demo.sh` starts an instance on it with `model_stub.py` as its model server |
| `config/` | shipped config: `celine.toml`, `llm.toml`, `kb.toml`, `skills.toml`, `mcp.toml`, `context.toml`, `agent/` (the agent's shipped name, pronouns, persona, talk, voice), `guard/` (the guard's persona, talk and judge prompts), `notebook_seed/` (first notes, copied once), `image_presets/`, `music_presets/`, `schedules/`, `searxng/` |
| `connectors/discord/` | discord-link, the Discord connector (Python) |
| `wa-link/` | the WhatsApp connector (Go) |
| `shellbox/`, `shellbox-manager/` | the agent's Linux container (shellboxd, the coding agent under `agents/`) and its reset |
| `browser/` | the agent's browser (Brave, browserd) |
| `plugins/`, `examples/plugins/` | the plugins that ship (the Mac) and two examples the tests load; yours go in the plugins folder ([docs/plugins.md](docs/plugins.md)) |
| `docs/` | `ARCHITECTURE.md`, `DEPLOY.md`, `dev.md`, `customize.md`, `plugins.md`, `ui/components.md`, `ui/words.md`, `ui/review.md` (the design criteria a screen is checked against) |

### server/celine

| Module | What |
|---|---|
| `__main__.py`, `config.py`, `boot.py`, `reload.py`, `saves.py` | preflight and lifespan; celine.toml into a Config and the readers' helpers; the whole load (every config file, var/settings, its row), which writes nothing; config reloaded in place; the Settings saves, one function each, for the routes and the agent's `settings` skill |
| `core/` | the single writer and event append, EventBus, ids and times, Services, `states.py` (turn states; the words a mission's, a tree node's and a run's state is said in) |
| `db/` | `migrations/0001_schema.sql`, the later `NNNN_*.sql` and `NNNN_*.py` (a `.py` one moves stored data), and the chains for `log/` and `notebook/`; `migrate.py` applies them |
| `conversations/` | conversation rows, the message tree, snapshot and replay, routes |
| `turns/` | one turn: dispatcher, runner, tool chokepoint, context, compaction, titles; `reply.py` the one place that touches a stored reply (citation tokens only); `warm.py` keeps the newest chat ready on its model server |
| `backends/` | model APIs (openai, anthropic), router, queue, the test model is `tests/fake_backend.py` |
| `skills/` | the tool registry and the tools without a module of their own; `guides/<skill>.md` the skill guides; `recall.py` what comes back on its own |
| `kb/` | knowledge bases: the chat archive, documents, instant and semantic search, the connectors' feed |
| `settings/` | `var/settings` and `var/secrets`: shapes, validation, `registry.py` (every setting), `features.py` (the feature catalog and probes), `services.py` (box addresses), `prompts.py` (Settings, Advanced, Prompts: the user's edits in var/prompts) |
| `agent.py`, `prompts.py` | the agent's row: name, pronouns, persona, talk, voice, portrait; `text(__file__, name)` reads a `.md` beside the module that sends it |
| `missions/`, `sub_agents/` | missions and sub-agents: `sub_agents/skill.py` the tool, `report.py` the report up, `cli.py` an agent CLI in the box, `drone.py` opencode on a device, `api.py` the routes |
| `devices.py`, `plugins.py` | the agent's devices (plugins with a `[drones]` block); plugins, base and yours, and the keys of a `plugin.toml` (MANIFEST) |
| `plans/` | the plan, one per conversation |
| `schedules/`, `triggers/` | routines: schedules and their runs (a failed run runs again only as its "When a run fails" setting says, or by Retry); watch and event triggers |
| `intercom/` | messages between everyone: rows, delivery, threads |
| `guard/` | the guard: one switch, one module per concern, prompts in `config/guard/` |
| `bugreports/` | bug reports the agent files, each an intercom thread |
| `homes.py` | where a conversation lives and the link that opens it, and the one walk up the agent chain (`root`); the client never works it out |
| `push.py` | web push: the VAPID key, the devices that take pushes, every notice to them while no page is focused |
| `jobs/` | steps that outlive their turn |
| `work.py`, `hold.py`, `leftovers.py` | the queue of background model jobs; why the agent's turns may not start now; what a done mission leaves open |
| `events.py`, `texts.py`, `approvals.py`, `waits.py`, `metrics.py` | the events list; every long text's history; approvals before something runs; Waiting for you, everything that waits on the user; Prometheus numbers |
| `attachments.py`, `box_files.py` | files on a message; files between the app and the box |
| `comfyui/`, `image_catalog.py`, `vision.py` | images and music; the model catalog; one question about one image |
| `terminals/`, `browser/`, `vault/`, `artifacts/`, `shares.py` | terminals, browser, vault (bw), pages, and shares of its box at /share/<token>/ |
| `mcp.py` | MCP servers as skills |
| `shellbox.py`, `speech.py` | the app's client for its shellbox; the speech HTTP client |
| `commands/` | slash commands the composer runs |
| `auth/` | login: passkey, password, PIN |
| `api/` | HTTP routes and the WebSocket |
| `clock.py`, `errors.py`, `logs.py`, `ops.py`, `keys.py` | time zone; errors; logging; `celine doctor/export/purge/keys` |

### web/src

| Path | What |
|---|---|
| `App.tsx`, `main.tsx` | routes, the rail, the socket, the global overlays |
| `ui/` | the kit; `ui/index.ts` exports it; `ui/ui.css` styles it |
| `views/` | one place each: `Celine`, `Missions`, `Routines`, `Notebook`, `Documents`, `Chats`, `Intercom`, `Waiting`, `Terminals`, `Terminal`, `Browser`, `Events` (`EventDecision`: one decision of the guard's), `Search`, `Artifact`, `Welcome` (`WelcomeForms`: the forms of its steps and of Settings, Features), `settings/` |
| `components/` | app parts made of kit parts: Transcript, Composer, ToolRow, ConversationList, ConversationPanel, Markdown, Plan, MissionAsk, ProviderFields |
| `lib/` | api clients, socket and bus, `nav.tsx`, `prefs.ts` (this device's conveniences in localStorage), `states.ts` (a state as the server says it), hooks (`useResource`, `useAction`, `useReveal`, `useConversation`) |
| `styles/` | `tokens.css` (every value), `base.css` (keyframes, resets) |

## Words

| Word | Means |
|---|---|
| agent | the agent each user creates and names (`agent.py` holds its row: name, pronouns, persona); the code, texts and docs call it the agent, its name is `{{name}}` in texts |
| the box | shellbox, the agent's Linux container |
| the house | the app itself and what runs inside it |
| outside | text from anyone but the user: web pages, mail, chats; untrusted |
| lands | a row or event arrives where it belongs |
| Bastet | the guard (`guard/`) |
| run, mission, job | a routine's run at one slot; a goal the agent works on with sub-agents; a step that outlives its turn |

## Gates

```sh
uv run pytest -q
uv run ruff check
cd web && npm ci && npm run build     # prebuild: the src/lib/*.check.mjs checks, eslint (hooks); then tsc and vite
cd wa-link && go test ./...           # when wa-link changed
```

An instance on a scratch data dir: `CELINE_DATA_DIR=DIR/data ./start.sh --port 8801`.

## Rules

1. No backwards compatibility: no shims, aliases or old shapes. Stored data changes once, in a migration, and the old code goes in the same change.
2. An interactive turn that fails fails with its reason. Nothing re-asks behind the user's back; a routine waits for its model; a failed run runs again only as its schedule's "When a run fails" setting says, or when the user presses Retry on it (a failed, skipped or stopped run), as a fresh run of its prompt. A model's output is stored and shown as it came: no code transforms, trims, replaces or rewrites a reply (`turns/reply.py` touches citation tokens only; known secrets are masked). A utility call's answer (a title, a summary, a judge's verdict) is no reply: the code that asked takes the part it needs, and a judge's words go into the app's own line under Rule 9.
3. The server owns state: lists, counts, labels, paths and effective settings come from the server through explicit events. The client never guesses.
4. Routes are plain REST: `/api/<plural-noun>[/{id}[/<sub-resource>]]`, kebab-case, a verb only as a trailing action (`/run`, `/stop`, `/test`), singular only for a singleton (`/api/agent`, `/api/status`), sockets under `/ws/`. Two exceptions: `/api/intercom`, which host scripts call as documented, and the model proxy's paths under `/llm/`, which follow the model vendors' APIs.
5. A text a model reads lives beside the code that sends it (`prompts.text`), or in `config/` when a user is meant to edit it, or edited by the user in Settings, Advanced, Prompts (var/prompts), which `prompts.text` reads first.
6. A feature is one entry in `settings/features.py`; a service address is a setting with a probe.
7. UI: a screen passes docs/ui/review.md; draw with the kit in `web/src/ui`; words from `docs/ui/words.md` or an icon with a tooltip; no explainer text; every colour, size, radius, shadow and duration from `web/src/styles/tokens.css`; one design. A dropdown only for a list that can run to hundreds of entries (a provider's models).
8. The page never scrolls: a screen fits the viewport and only inner regions scroll (a transcript, a list, a text box, a card body).
9. No em dash, en dash or " · " in any text a user or a model sees. Code that needs one writes the escape. Text says what exists and what it does, never what does not exist. Comments get to the point with no preamble, in plain technical English that has one reading. A line the app adds (an error, a run's reason, a refusal, a note it posts in a thread) reads as the app's own: it names what the app found or did, in one sentence, and never reads as if a person wrote it.
10. Tests use fixed clocks and wait on conditions, never on sleeps.
11. Small commits with short lowercase messages. Every gate green before a push.
