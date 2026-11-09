# Architecture

## Shape

One Python process. The app's state is one SQLite file with one writer thread; every
knowledge base is another SQLite file of its own (see Knowledge bases). A React app served as static
files from the same process, or by Vite in dev. Turns run server side and survive the
browser going away. The WebSocket is a tail of a durable event log; closing it changes
nothing server side.

```
celine-net/
├── start.sh                    ./start.sh | dev | --port N
├── config/                     hand-written TOML (see Configuration)
├── var/                        gitignored, created on first run
│   ├── celine.db               the database
│   ├── log.db                  the logs: events, the global feed, requests, raw responses
│   ├── settings/, secrets/     what Settings writes; API keys, mode 600
│   ├── blobs/                  attachments, content-addressed
│   ├── notebook/, documents/   its notebook (its index .index.db in it); your Documents
│   ├── kb/<id>.db              one SQLite file per knowledge base
│   └── logs/celine.log         json lines, rotated at 10 MB
├── server/celine/              the app (module table in AGENT.md)
│   ├── core/                   the single writer and event append, EventBus, Services
│   ├── conversations/          rows, the message tree, snapshot and replay, routes
│   ├── turns/                  dispatcher, runner, tool chokepoint, context, compaction, titles
│   ├── backends/               model APIs, router, queue
│   ├── skills/                 registry, tools without a module of their own, guides/
│   ├── kb/                     knowledge bases
│   ├── db/                     migrations/ and migrate.py
│   └── api/                    shared HTTP routes and the WebSocket
├── connectors/discord/         the Discord connector (Python)
├── wa-link/                    the WhatsApp connector (Go)
├── tests/                      pytest
└── web/src/                    App.tsx, views/, components/, ui/ (the kit), lib/, styles/
```

Each module keeps its rows next to its code (`conversations/store.py`,
`turns/store.py`, ...) as plain functions over the core `Store`; there is no central
query file.

## Startup

`__main__.preflight` runs in this order; a refusal is one sentence and exit code 2.

1. `WEB_CONCURRENCY` must be 1. A second process means a second writer.
2. Config is loaded: `.env`, then `config/`, then `var/settings/`. A file that does not
   parse (any `config/*.toml`, any `var/settings/*.toml`) is one line naming the file,
   line and column, and exit 2; a missing optional file is its defaults. An unknown table
   or key, or a value of the wrong type, is a `Problem` naming the file and the key
   (Settings, General "A setting needs a fix", the log at start, `celine doctor`); the key is dropped and
   the reader's default counts. The key lists live in `config.py` beside the comments that
   name their readers. Logging starts and every known secret goes on the redaction list.
3. The login: with `[server] login = "off"` the host must be loopback; with
   `"on"` the public origin must be one passkeys work with (`auth/`, docs/DEPLOY.md
   "Login").
4. Port is checked so "already running" is a sentence and not a traceback.
5. `var/celine.db`, `var/log.db` and `var/notebook/.index.db` are migrated, each its own chain.

Then the lifespan starts the parts in order: the store; the LLM queue and its Router
(before anything that may ask a model); the knowledge bases and their schedulers; the
skills; the MCP supervisor and the connectors; the sweep of turns a crash or restart
left behind (see Durable execution); the dispatcher's workers; the intercom and its
timer; triggers; the scheduler; the terminal sync; the config watcher; the
login's sessions; the health port.

Everything the app runs is one `Services` object (`core/services.py`), made with the app
and filled by the lifespan as each part starts. Skills get it as `self.app` from
`skills.build` and read a field when a tool runs; the routes read it as
`request.app.state.services`, the only other state being the login (`app.state.auth`)
and the allowed origins.

Flags: `--dev` accepts the Vite origin, `--port`, `--verbose`.

## Configuration

TOML under `config/`: hand-written, the app only reads it (read-only in Docker).
Providers, models, the model roles (which model does what), tool defaults, the time zone
and API keys are Settings, written to `var/settings/*.toml` and `var/secrets/`; a fresh
install has none of them (nothing shipped). A conversation's own model, thinking level
and tool switches live in the database.

`context.toml` is a shipped default: it is copied once into `var/settings/` when its
place there does not exist (`settings/seed.py`), then only `var/settings/` is read.
`agent/` (agent.toml, persona.md, owner.md, voice.toml, talk.md) is the agent's shipped base:
a fresh install's row starts from it (`agent.seed`). The agent's name (`{{name}}` in prompt text)
and pronouns are its row's; your name (`{{username}}`) is Settings, General.

`celine.toml` is how the app is served, read at start: `[server] host`, `port`
(127.0.0.1, 8766; `CELINE_HOST`, `CELINE_PORT` win), `login`,
`allowed_hosts`, `trusted_proxies`, `health_port`, `metrics_port`, and `[paths] data`
(var). `config.load` reads it and writes nothing; `boot.load` reads the rest
(mcp.toml, kb.toml, skills.toml, llm.toml, the presets, var/settings, its row), each
file's keys checked against the table of its owner (`mcp.KEYS`, `kb.TABLES`,
`skills.BUILTIN` with `enabled` only, `backends.llm.QUEUE_KEYS`). The data dir, the stack's keys and the
seeded settings are made once by `__main__.preflight`, before the first full load.

The Advanced settings (Settings, Advanced; `settings/general.py` FIELDS, saved in
`var/settings/general.toml`) are the time limits, sizes and switches, each defined once
with its label, type, range and default and read where it is used with `cfg.r(key)`, so
a save applies at once. They are listed here in the page's sections (`general.GROUPS`):

| Key | Label | Default | Meaning |
|---|---|---|---|
| `max_concurrent_turns` | Turns at once | 6 | conversations generating at once (the dispatcher's workers follow it, plus one for each turn that waits for your answer) |
| `max_tool_iterations` | Tool rounds per turn | 0 | model/tool round trips per turn; 0 means no cap, the wall clock is the limit |
| `turn_wall_clock_seconds` | Turn time limit | 1800 | its work time in one turn, tools included |
| `last_step_seconds` | Last answer time | 180 | after the work time: the step under way may still finish |
| `tool_timeout_seconds` | Tool time limit | 180 | a blocking tool past this is cancelled |
| `job_timeout_seconds` | Job time limit | 3600 | a background job is stopped after this; must exceed `tool_timeout_seconds` |
| `verify_citations` | Check citations | on | the citation check of a reply (`turns/reply.py`): every message or document it cites must be one a tool showed |
| `embed_query_timeout_seconds` | Search embedding time limit | 60 | how long a search waits for the Topic search model (`kb/semantic.py`) |
| `busy_retry_seconds` | Busy model wait | 10 | agent and background work asks a busy model again after this, doubled at each try |
| `busy_retry_max_seconds` | Longest busy model wait | 600 | the longest wait between two asks of a busy model; at least `busy_retry_seconds` |
| `routine_max_minutes` | Routine time limit | 180 | the most minutes one routine run may work; 0 means no limit (Schedules, When a run fails) |
| `routines_wait_for_missions` | Routines wait for missions | off | a routine run starts only while no turn of a mission is live (Schedules, When a run starts) |
| `mission_max_hours` | Mission time limit | 0 | the most hours one mission may take from its start; 0 means no limit (`missions/watch.py`) |
| `mission_max_sub_agents` | Sub-agents per mission | 0 | the most sub-agents one mission may start; 0 means no limit |
| `missions_resume_after_unfreeze` | Continue missions after Unfreeze | on | a mission a freeze stopped continues by itself after Unfreeze |
| `max_attachments` | Attachments per message | 10 | per message |
| `max_upload_mb` | Largest upload | 512 | MB of one file added to Documents |
| `max_attachment_bytes` | Largest attachment | 20000000 | refused during upload |
| `inline_text_max_bytes` | Largest text read inline | 200000 | larger text attachments become a note |
| `max_message_bytes` | Largest message | 1000000 | enforced on POST |
| `document_part_mb` | Largest part in a document | 256 | an EPUB or DOCX part that unpacks larger is not read |
| `document_html_mb` | Largest web page document | 64 | a larger HTML document is stored, not read |
| `document_pdf_pages` | PDF pages read | 5000 | pages read of one PDF |
| `document_pdf_seconds` | PDF read time limit | 600 | time spent reading one PDF, looked at between pages |
| `events_keep` | Log entries per chat | 500 | log entries kept per conversation once it is idle |
| `backup_keep_days` | Backups kept | 7 | nights of backups kept |
| `requests_keep` | Model requests kept | 0 | days a model request body is kept in log.db; 0 keeps it forever |

FIELDS also holds the settings shown on other pages: `user_name` (Your name, default
"User"), `timezone` (Time zone, UTC) and `user_bio` (About you) in General's You section
and the onboarding; `lock_archived` (Lock archived chats, on) and `image_captions_auto`
(Describe new photos in Messages, off) on General; `smart_search_refresh_minutes` (Update
the search index every, 15 minutes) on Search index; `ask_before_setup` (Ask before
setting up services, on) and `ask_listed_cloud` (Ask before cloud models from your lists,
off) in the agent page's Behaviour section; `guard` (Guard mode, Warn) on Guard; and
`code_websearch` (Web search for the coding agent, off), which no page shows (general.toml
or `settings__propose`).

A skill's own numbers are declared beside its code: its module (the one `BUILTIN` names)
has `KNOBS`, a tuple of `skills.knob(key, label, unit, min, max, default, help)` with keys
`<skill>.<name>` and the default as the constant. `settings/general.py` `fields()` adds
the knobs of every skill whose module is there (`skills.module`, `skills.knob_fields`)
after FIELDS, advanced, with `group` the skill's label (`tools_meta.SKILLS`); Settings,
Advanced shows the app's own first and then one section per group. They are read at use
with `cfg.r("web.results")`. `general.toml` keeps them in a table per skill (`[web]
results = 10`, `section_to_file`), and `validate` reads a table back as its dotted keys,
refuses an unknown knob of a skill that is there and drops the table of a skill that is
gone. Sizes are in MB (1024 * 1024 bytes). The knobs: `notebook.index_max_entries`,
`notebook.index_max_chars`; `past_conversations.max_results`, `snippet_chars`;
`sub_agents.max_running`, `cli_minutes`; `code.poll_seconds`, `result_chars`;
`chat_archive.search_max_tokens`, `search_max_tokens_all`, `candidates`, `rerank_floor`,
`episode_gap_minutes`, `session_max_tokens`, `period_max_tokens`, `max_tokens_cap`,
`scroll_max_messages`, `scroll_max_tokens`, `get_max_refs`; `web.results`,
`search_timeout`, `per_hour`, `read_tokens`, `timeout`, `page_max_mb`, `pdf_max_mb`. Each
has its help, the "?" on Advanced.

`skills.toml` holds one section per skill with `enabled` and nothing else. `build`, the
reload and the knobs all walk `skills.BUILTIN` and skip a skill whose module is gone
(`skills.module`: None for a missing file or folder, any other import error raised), so
a skill's module or folder removed takes the skill, its tools and its knobs with it.

Models: `var/settings/providers.toml` (providers and their models) and `roles.toml`
(an ordered list of models per role), kept as `cfg.settings`. Which model answers a call
is decided at call time by the Router (`backends/router.py`, "The model queue" below).
A `.env` at the project root is loaded before the config (`KEY=VALUE`, quotes
optional, variables already set win, never logged); `mcp.toml` reads it through
`${VAR}`, `${VAR:-default}` and `${VAR-default}`. `.env.example` lists the names. API
keys for model providers are typed into Settings (`var/secrets/`), never read from the
environment. `CELINE_DATA_DIR` overrides `[paths] data`, for a
second instance beside the real one. `CELINE_CONFIG_DIR` does the same for `config/` (tests, a second instance).
The chat model is the Chat role in Settings (`PUT /api/roles/chat`). A
conversation can pick its own (`PATCH /api/conversations/{cid}/settings`).

Service addresses (`settings/services.py`, `var/settings/services.toml`; each service's
form in Settings, Features and Setup): the shellbox, the browser and web search, one
`url` each, saved on its own with `PUT /api/services/{id}` `{url}`. A saved address wins
over the compose's variable (`CELINE_SHELL_URL`, `CELINE_BROWSER_URL`,
`CELINE_SEARXNG_URL`), an empty one too; a service with none saved takes the variable,
read on every call. `{url: null}` forgets the saved address (Use the default), so the
variable applies again. `GET /api/services` gives each service as `{url, source,
default}`: `source` saved, environment or default, and `default` the address Use the
default puts back while a saved one hides it, else null. While `default` is set, the
service's form shows a Use the default icon button. Migration 0024 dropped each saved address equal to its
variable (DEPLOY.md, Updating).

Who it is and how it sounds are fields of its row (Settings, the agent's page): `persona`, `talk`
(null: the shipped `config/agent/talk.md`) and its Banned words (`voice`). Every turn of
its own gets all of it, whatever the kind: a chat, a mission, a sub-agent,
a background run (`prompt.system_blocks`, the one assembly). It reads them with
`me__read` and changes its persona, its talk and its banned words with `me__edit`
(`skills/me.py`). Every edit waits for your approval first (`approvals.py`, kind
`agent`), whether the guard is on or not. Every change goes through `agent.write_sync` into
`text_versions` as `agent:persona`, `agent:talk` and `agent:voice`, and `POST /api/text-versions/{vid}/revert`
makes a version the text again.

Settings, Advanced, Prompts (`settings/prompts.py`, `api/prompts.py`) edits every package text a
model reads, each skill guide and Bastet's judges. `GET /api/prompts` gives the groups
(its name, App, Skill guides, Guard; the persona, example lines, banned words and
Bastet's three texts are rows that open their own page; a guide's row says its
frontmatter description) with `help`; `GET /api/prompts/{id}` one text; `PUT
/api/prompts/{id}` `{text}` saves the user's edit as `var/prompts/<id>.md`, which
`prompts.text` (and `guard/prompts.py` for a judge) reads before the shipped file, at its
next use. A save is checked first (`settings/prompts.py problem`, else 400 with the
sentence): a judge keeps the slots of its shipped text, and a guide keeps `name: <stem>`
in its frontmatter. `{text: null}`, or a text equal to the shipped one, removes the edit
(Use the default). Every save is kept in the text's History (`texts.py`, `prompt:<id>`,
the shipped text its first version), drops that text's "nothing fills" problem
(`prompts.settled`) and sends `settings.changed` (`what: ["prompts"]`). The page opens a
text at `/settings/prompts/edit/<id>`, its History at `.../history`. An app update
changes only the shipped files, so an edited text stays as the user wrote it.

It reads Settings and proposes changes to them (`skills/settings.py`): `settings__read` gives
every target Settings saves at once (features, general, roles, a provider, tools,
services, email, the vault, the context shares, a plugin's form) as fields with values,
allowed values and a version, a secret only as its state. `settings__propose` checks a
change with the validators of a save (`settings/write.check`), waits for your approval
(kind `settings`: each field before and after, and its why) and saves it with the
Settings screen's own function (`saves.py`) on the version it was proposed
against; a target that changed meanwhile is not overwritten. A secret comes from a file
in its shellbox, so its value never passes its context, the request, an event or a log.
The skill is not core: its tools go by name, so their parameters cost nothing in the
turns that do not touch Settings.

A llama-swap, tabby or llama-server model's `llama` block in Settings goes into the
request body; `llama.grammar.file` (relative to `config/`) is sent as `grammar`. The
Banned words (its row's `voice`, shipped `config/agent/voice.toml`) are the list of words
its turns are pushed away from. `LLM.make` puts them on the BackendConfig for its turns
and `LlamaServerBackend.request_body` sends them: as `banned_strings` on tabbyAPI, as
`logit_bias` on llama-swap or llama-server. Tool, vision and embedding calls never carry
them.

`mcp.toml` is parsed into `mcp.McpServerConfig`. Unknown keys and wrong types in every
config file are reported.

`reload.py` watches `config/` and `var/settings/`, and every save in Settings runs the
same step. The files are loaded again and, unless a problem is fatal, the values are
copied into the live `Config` object in place, so the dispatcher, runner, Router and
skills see them on their next read; a turn waiting for a model looks again at once.
Every built-in skill is rebuilt (a skill switched off leaves the registry) from the one
`BUILTIN` list and gets the same `Services` object, so it needs no rewiring. `mcp.toml`,
`[server]` and `[paths] data` need a restart, and say so as a problem. The triggers start
again with the new state, and the dispatcher's workers follow Turns at once. A file that
does not parse refuses the reload (`config.reload_failed`, its message a fatal problem)
and the last good values stay. No model roles is not a problem.

## Database

Three files. `var/celine.db` is the state: `db/migrations/0001_schema.sql` is its whole
schema. `var/log.db` is the append-only part, pruned by count: `events`
(each conversation's log), `global_events` (the feed every page follows), `requests` and
`raw_responses` (what went to and came from the model), `terminal_commands`;
`db/migrations/log/0001_schema.sql`. Every file keeps its version in its header
(`PRAGMA user_version`). The notebook's index
(`notebook_index`, `notebook_fts`) is `var/notebook/.index.db`, beside the notes and
backed up with them; `db/migrations/notebook/0001_schema.sql`; startup's reconcile
rebuilds it from the files. The `Store` opens celine.db and attaches log.db as `log` and
the index as `notebook` on the same connection, so a write spans them in one transaction
and a read joins across them in one SQL; the attached files have no foreign keys into
celine.db, so a purge deletes their rows by name (`conversations/store.py`,
`terminals/store.py`). WAL, foreign keys on, busy timeout 5 s. A file older than its
first migration stops the server; later numbered migrations apply on top, in one
transaction each ([dev.md](dev.md)). Before a migration changes a file with data, the
file is copied beside it as `<file>.pre-<version>.bak`; the last three are kept.

Main tables: `conversations`, `messages`, `tool_calls`, `turns`, `events`,
`global_events`, `jobs`, `compactions`, `context_state`, `attachments`, `blobs`,
`notebook_index`, `terminals`, `missions`, `artifacts`, `intercom` and its deliveries,
`requests` (every model request a reply's steps sent, zlib-compressed, with the server's
token counts and the known secrets masked; `GET .../messages/{mid}/requests`), and `agent`
(the agent's one row). Each module keeps its rows next to its code as plain functions over the
core `Store`.

`messages.author` is `user` (the user's own message as typed), `model` or `intercom`
(everything else that is not its own); every user-role message has `intercom_id`, the
intercom row it was delivered from. `messages.meta` is json with only what is not the
default: on a reply, `thinking` (the thinking blocks its provider signed; read only by
the context view, never sent to clients; an edited or continued version takes the meta
without it, since a signature belongs to its text) and `quiet` from the message's turn
(`turns.quiet`: a turn that shows only its tool calls). A deleted message is gone at once,
with every message after it in its branch; there is no message trash.

### The event logs

Two durable logs, both written only by the single writer (`core/store.py`, a thread of
its own; the loop queues a write and awaits it, and the write's events are published on
the loop after its commit):

- A conversation's: `events(conversation_id, seq)` is unique and gapless per
  conversation. The counter is `conversations.next_seq`, bumped in the same transaction
  as the insert (`Store.event(c, cid, type, data)`).
- The global feed: `global_events(id, type, data, created_at)`, its own gapless id.
  Everything every open page hears goes there: `Store.global_event(c, type, data)`
  inside a write, `Store.emit_global({"type", ...})` from outside one. A conversation
  event whose type is in `core/bus.py MIRRORED` is copied onto the feed in the same
  write, with `conversation` and `seq` and without its payload: ids, not results.

`Store.write(fn)` and `Store.read(fn)` take a sync callable and run it inside one
transaction; a caller cannot await mid-transaction. Events of a write are published to
the `EventBus` after COMMIT. A write after the store closed raises (`store is not
running`).

At startup and every ten minutes after (`keep_pruned`), `prune_events` drops all but the last `events_keep`
entries of every idle conversation and raises `trim_below`, keeping what the snapshot
reads back (`images.notice`, `llm.queue` with `fallback`); the global feed keeps its newest
20000 rows (`GLOBAL_KEEP`). A client resuming from below gets `resync`. At startup it
also drops the `requests` older than `requests_keep` days (0, the default, keeps them).

### The message tree

`messages.parent_id` is the tree. `ordinal` is sibling display order and nothing else;
there is no unique constraint on it. `conversations.active_leaf_id` is where the visible
path ends, `messages.preferred_child_id` is which child to follow when descending.
History for the model is a parent-pointer walk from the active leaf, from the newest
summary on (`turns/context.py`). `reasoning` is stored on the row and
never sent to a provider.

A version of a message is a sibling of it (same parent); the counter and the arrows are
`GET .../messages/{mid}/siblings`. Nothing is overwritten: Retry, Regenerate, Edit and Continue
each write a new sibling and move the active leaf to it (`PUT .../active-version` brings an old
one back).

- **Edit** (`tree.edit`): a user message's new version is answered by a queued turn. The agent's
  reply's new version is a sibling of its last step with the new text; the steps above
  it, its tool calls among them, are the same rows.
- **Continue its reply** (`tree.continue_message`): its finished reply at the active leaf
  gets a new version holding its text, and a queued turn rooted at its parent continues
  it in place (see Continue). Refused with `{refused}` for a message that is not its own,
  not the active leaf, has no text, or while a turn runs.

## A turn

`POST /conversations/{id}/messages` sends the user's message on the intercom
(`Intercom.send` from `user` to `agent:<id>`, see Intercom): an idle conversation gets the
message and a `queued` turn at once and the dispatcher is notified; while a turn runs the
message reaches it at its next step. It returns `{intercom_id, message_id, turn_id, seq}`,
the last three null when it did not land at once. New in the UI only clears
the selection; the conversation row is created on the first send. The title comes
after the first reply: the dispatcher puts a `title` work item (see The work queue)
that asks the small model for a 2 to 4 word name (`turns/title.py`), stores the first
line it wrote as it came, and appends `conversation.title`. A failure leaves the title
empty and logs why.
`POST /conversations/{id}/regenerate` queues a fresh turn for the last user message when
nothing with words answered it: the store walks up from the active leaf past tool
messages, empty assistant messages, a failed turn's text and quiet notices, moves the
leaf back to the user message and roots the turn there. A reply with text raises 400.
While no turn is live, the snapshot and every `turn.state` event carry that message as
`unanswered: {message_id, turn_id, state, reason, continues}` (`tree.unanswered`: the
turn that tried, how it ended and why, in a sentence), null otherwise; the transcript
shows the reason under it with Regenerate. `continues`: that turn failed after its reply
said something, Continue can pick it up (`view.continuable`) and the conversation's chat
model continues a reply in place (`tree.prefills`, set at start to
`conversations/api.prefills`); the transcript then shows Continue beside Regenerate.

Workers (`max_concurrent_turns` of them, plus one for each turn that waits for your
answer to a request: a waiting turn takes no slot) lease conversations: `turns.claim` picks a
queued turn whose conversation has nothing in `preparing`, `streaming` or `tools`: the
user's turns (`origin='user'`) first, then the oldest. A worker sleeps until `notify()`,
which every write that queues a turn calls (no poll). When `turn_wall_clock_seconds` of
work are over, no new tool call starts and a running one is stopped; no new step starts,
and the turn ends `capped` with `reason_code` `wall_clock` (a Stop ends it `aborted`). Time spent waiting for a model does not count. Past the work time plus
`last_step_seconds` the turn is `failed` with the reason and the conversation is free.

States: `queued`, `preparing`, `streaming`, `tools`, then `done`, `aborted`, `failed`,
`interrupted`, `capped`. Every transition is a write, and every write re-reads
`abort_requested`. A terminal state carries `reason` (words) and `reason_code`
(`stopped`, `restart`, `crash`, `wall_clock`, `max_tool_iterations`, `backend`,
`backend_retryable`, `context_too_small`, `cut_length`); a `done` reply that
hit the output limit has `cut = 'length'`. **Every transition into a terminal state
settles the calls the turn left open** in the same write: each `pending`/`running` call
becomes `interrupted` with a `role=tool` row and a `tool.done {state: "interrupted"}`
event. Calls record what the tool was (`tool_calls.mutating`, `mode`).

`runner.run_turn`:

0. Find where the turn stands (`turns.resume_point`): a fresh turn starts at its user
   message; a resumed or continued one carries on from its own tail on the active
   path, with the next free iteration number (so call ids never collide with the
   journal). Read-only calls it left in flight run again with the same ids; a reply
   without calls at the tail is continued in place (below).
1. Resolve the tool list once. A tool disabled for this conversation stays in the list.
2. Build the system prompt (`turns/prompt.py`): its persona, who it is talking to, where
   it is, how it talks (`talk`), the notes it always carries, the skill index and the
   core skills' fragments. The user message the turn answers gets the turn's tail once
   (`prompt.tail_text`: the time, its plan, what is off or down, the skills' live state).
   `/compact` and `/usage` build the same prompt (`prompt.request_parts`). Then the request, through the context
   builder (`turns/context.py`, below): old tool results may go as stubs and older turns
   as a summary.

3. Stream. Text deltas are appended to the log as they arrive; the message row is
   written once at the end with the full text. `message.done` carries the full text and
   the client replaces its accumulation with it; when the citation check or the links
   change the text afterwards, `message.rewritten` carries the final text, once. The
   whole chain is `turns/reply.py` (`stored`): its words are never changed, only citation
   tokens are made canonical, marked `|invalid` or turned into their link, in the order
   its docstring lists.
4. If the model called tools: record them, run them through `tools._resolve`, append
   the results to the wire history, loop. Before every model request the runner takes
   what the intercom has due for the conversation (`intercom/deliver.take`), so a message
   sent while it works reaches it at its next step. Stop at `max_tool_iterations` when
   it is set, or the wall clock (`capped`). Several tool calls in one reply run in order.

Tool call ids are `turn:iteration:index`. A step reached again after a crash finds its
own result in the journal and is replayed; it never runs twice.

### Continue

`POST /conversations/{id}/turns/{tid}/continue` puts an unfinished turn back in the queue
**as the same turn** (`turns.continue_turn`, one write). Continuable: the conversation's
latest turn, nothing else running, the active leaf still in this turn, and state
`interrupted`, `aborted`, `capped`, `failed` with a retryable code (`backend_retryable`,
`crash`, `wall_clock`, `no_model`, `busy`, `context_too_small`) or `done` with
`cut = 'length'`. The write
resets the tail reply's `interrupted`/`cancelled` calls to `pending` **only when the tool
is read-only and blocking**; a mutating one keeps its `interrupted` result and is never
re-run silently. It bumps `turns.continues` and appends `turn.state {queued, reason:
"continued", continues}`. With `{"wait": true}` (Keep waiting) the reason is
`keep_waiting`: the turn's model calls wait for a busy provider until Stop.

The runner then continues by what the tail holds: pending read-only calls run (same ids);
a reply with text and no calls is **continued in the same message**: the request ends
with that partial reply as the final assistant message (prefill), and
`message.reopened {message_id}` reopens it. A model without a prefill cannot continue it:
Continue is refused before anything is written, and a turn queued again by a mission or
a routine fails with `reason_code = 'no_prefill'`, the reply as it was. An empty
reply is streamed into as a new step.

The snapshot's `turn` carries `continuable: {ok, label, reason}` (computed on the
server), plus `reason_code`, `cut`, `continues`. Terminal `turn.state` events carry the
same `continuable`.

### Shutdown and restart

At shutdown the app first sends a `notice` from `system:app` to every conversation with
a running job (`wake` off). Then `Dispatcher.stop()` runs before the store closes: every
running turn's token is cancelled with reason `shutdown` (not a Stop: streams keep their
partial text, a step that said nothing goes). It waits up to `shutdown_grace_seconds`
(10 s) only while a step with a side effect is in flight. Then `store.interrupt_held`
ends every turn this process held `interrupted` (`reason_code = 'restart'`), its open
calls settled, with the reason in a sentence (`journal.restarted` names a step with a
side effect that was in flight).

At startup, before the workers start, `turns.sweep(store, registry)` ends every turn a
crash left running the same way; queued turns wait for a worker. Nothing runs again on
its own: the user's message shows its reason with Regenerate (the snapshot's and every
`turn.state` event's `unanswered`), or Continue picks the turn up. Running jobs end
`interrupted`; a conversation one of them was cut in gets one `notice` from
`system:app` saying what was cut. The runner refuses to re-run a mutating step it finds
pending.

### Token accounting

`turns/tokens.py` is the only place that turns text into tokens: characters per token by
kind, times a per-model correction learned from every call's `usage.prompt_tokens`. The
window is what the server says (`/upstream/<model>/props` `n_ctx`, an overflow error
body, else the provider's model list, `Router.discover`); the model's context size in
Settings counts only where no server says and on Ollama; fallback 32,768. One tool
result takes at most `result_tokens` of context.toml (10,000 by default) or 15 % of the
window, the smaller (`tokens.result_cap`, `ToolContext.max_output_tokens`;
`tools._capped` keeps its start and its end with a `[cut: ...]` line between). Every model call appends
`context.usage {turn_id, iteration, estimate, prompt_tokens, completion_tokens, ...}`
(log only) and bumps `turns.iterations`. `finish_reason = "length"` on a final reply
gives `done` with `cut = 'length'`.

### Context: shortening and summarising

`turns/context.py` builds every request of a turn (`build(mode=...)`) from the active
path (`compactions.context_view`: the chain, every reply's calls with their stored
results, the aging mark, the valid compactions). **The database never shrinks for the
model's sake**: stored rows, the UI and citations keep the full text; only the request
changes. The prompt prefix must stay stable (llama.cpp reuses its KV cache,
`cache_prompt`, `--cache-reuse 256`), so every decision is made in large steps and
persisted, and the same prompt is rebuilt byte for byte after a restart.

Order: `system` = persona + skill fragments + (when a summary is in effect) "Earlier in
this conversation (a summary of N older messages): ..."; then the messages after the
newest valid compaction, tool results verbatim or, for replies at or before
`context_state.aged_through`, as a stub (`compact.stub`: the call, its first lines, its
size, up to 40 message refs, the lines whose refs it cited in a later reply kept whole).
`notebook__*` and results under 300 tokens are never shortened; a result that carries an
image is shortened like any other. The calls of those replies go with every string argument over
about 300 tokens shortened (`compact.short_arguments`): its first line, then "… [N lines,
about T tokens, shortened; the call is kept: <id>; read it back with
past_conversations__read_tool_call(id=...)]", the JSON still valid (a heredoc written
once does not ride along in every later request); `notebook__*` calls stay whole. Previous turns' calls go on the wire with their ids
(`tool_calls` on the assistant message, `tool_call_id` on the result). Every result
reaches the model with its tool's name (`backends/openai.wire_messages`; Ollama
`tool_name`; Anthropic ties `tool_result` to `tool_use` by id).

The agent's own-mind block at the end of a new user message, first line `[your mind]`
(`skills/mind.py`, stored once in `message_context`): its mood, the gap since the last
talk, a new model, what is down and an unfreeze. There is no block unless its state
differs from the last whole block on this branch, none is in its view (a compaction
lies between), the last message here is over an hour old, or
an unfreeze; then it goes whole.

The agent's memory block, before it in the same stored row, first line `[your memory]`
(`skills/recall.py`): the sections of the notes whose `keys:` line comes up in the
message or its last reply, graded by the cosine similarity of their embedding to the
talk's (`kb/semantic.query_embedder`). Only a section at or over `memory_floor` comes
back, the best first, at most `memory_sections` in `memory_tokens` tokens
(`var/settings/context.toml`); each entry is `### path#heading (similarity, last
changed)`. Not for a note it carries in full or with `load: off`, nor a section shown on
this branch since the last compaction unless it changed. An embedding model that does not
answer gives `[your memory] not available now: <why>` and nothing else. The report
(`report.notebook`) keeps the query and every candidate with its score and state.

What the build decides is kept per conversation: the aging mark (`context_state`:
`aged_through`, `turns`) and the summaries (`compactions`). The tail (rebuilt every
turn, never stored) goes after the last user-role message.

Every user-role message goes on the wire with its time first: the line `[Fri 2 Oct
01:40]` (`context.time_line`), then the header rendered from its intercom row
(`intercom/render.header`: `[From scout (#412, re #409, "logs")]`, `[Job 3f2a1b9c
(shell__run) ended]`, nothing for the user's own message), then the content.

**One mark for what the app adds.** Everything the app puts into what it reads is a line
in square brackets: the send time, the intercom headers, `[your mind]`, `[your memory]`,
`[your notes]`, shortened and cut results (`[earlier result, shortened to save room]`,
`[cut: ...]`) and a screen. The one exception is
outside text inside `<outside>` tags while Bastet is on. The legend under its persona
(`turns/legend.py`) names each mark; `tests/test_self.py` checks it names the marks the
code writes. The stored row stays bare. The agent's clock is the newest message's line, and
today's date ends its system prompt (`clock.today`).

Decisions use the whole request estimate (system + tool schemas + history) as a share
of the loaded model's window:

| When | Shorten old results | Summarise older exchanges |
|---|---|---|
| turn start (`start`) | above 60 %, down to 45 %, keeping the current exchange and the 2 before it; never for less than 4,000 tokens | still above 75 %: down to 40 %, keeping at least the current exchange and the 2 before it |
| right after a `done` turn (`background`) | same | above 60 %, so the next turn usually starts from a ready summary |
| between steps (`turn`) | next request above 85 %: down to 30 %, never the last 2 results, this turn's included | still above 75 % |
| the server said it does not fit (`emergency`) | all but the last result | down to the current exchange |
| `/compact [focus]` (`manual`) | nothing | everything but the latest exchange |
| `/usage` and `GET .../context` (`dry`) | reports what it would do | reports |

`GET /api/conversations/{cid}/context` ("what it sees") builds the next request the
same way, without a model call, and measures it in tokens with the same estimate:
`{window, shares: {notes, tools}, blocks: [{name, tokens}], notes: [{path, tokens,
state, why}], loaded: {notes: [{path, since}], skills: [{name, since}]}, tools}`.
`loaded` is what it carries now, each with the turns since a turn touched it. The blocks,
in order: `persona`, `talk`, `about`, `notes`, `tools` (the skill index, fragments and
tool schemas) and `history` (`conversations/api.py BLOCKS`). `notes` are the
notes it carries or would: `included`, `left_out` (over the share) or `off`, each with why.

Automatic summaries (not `/compact`, not emergency) are skipped when they would save
less than 4,000 tokens. The aging mark only moves forward and is ignored while its
message is not on the active path. A compaction (`compactions` row: `through_message_id`,
`trigger`, `covers_messages`, `text`, token counts, `focus`, `model`) is valid while
`through_message_id` is on the active path; the newest valid one is used. The summary is
**rolling** and ends with the refs of the summarised results it does not mention
(`compact.with_dropped_refs`), so nothing it found becomes unfindable.
The summary request goes to the conversation's chat model and no other, with no tools,
in pieces of at most half the window (`compact.PROMPT`). Nothing is ever left out in its
place: when the summary fails, a turn fails with the reason (`reason_code =
'summary_failed'`, `context.compact_failed` with `reason`), `/compact` says failed, and
the background build after a turn leaves the messages as they are for the next build.
One decision per conversation at a time (`context.lock`).

Every request message carries its `origin` (the app's key; no backend sends it): the
conversation, the turn, the stored message or the tool call, or the `note` the app
added for this request only (system, tail, opening, screen).
Before a step goes out the runner holds it to `context.malformed`: a tool result right
after the reply that made its call, each call answered once, no stored message twice,
nothing from another conversation, its first reply after a user message. A request that
breaks one fails the turn (`reason_code = 'malformed_request'`), it is never sent. The
next step of a turn hangs under its own last result (`turns.step_tail`), never wherever
the conversation's leaf is.

Overflow: the backend's `context_overflow` error is caught once per run
(`runner._Overflow`). Its n_ctx becomes the model's window (`tokens.learn_window`), the
builder runs in `emergency` mode and the same step is streamed again into the same
reopened message. The turn fails with `reason_code = 'context_too_small'`
(`runner._too_small`) when the instructions and tools alone do not fit, the request after
the emergency pass still does not fit, or the retried request overflows again.

Citations: a ref it was shown earlier stays citable after its result became a stub
or went into a summary (`Built.hidden`, the stored full text of every result not sent
verbatim, is checked with the request by `verify.shown_refs` / `web.shown`).

Events (durable): `context.compacting`, `context.compacted` (the divider),
`context.compact_failed`, `context.compact_skipped`, `context.aged`, `context.emergency`.
The snapshot carries `compactions` (the valid ones on the path). The numbers live in
`context.Policy`.

### Backends

Every backend takes OpenAI-shaped messages and yields the same BackendEvents:
`openai.py` (openai, openrouter, llama-server), `anthropic.py` (`/v1/messages`: system
messages become `system` blocks, tool calls `tool_use`, results `tool_result`, same-role
neighbours merge; signed thinking blocks go first in a reply while its thinking is kept
on the wire; prompt cache breakpoints on the last system block and the last history
block; `temperature` is not sent), and `ollama.py` (`/api/chat`, `num_ctx`). The stream is passed on as the
server sent it: a tool call the model wrote as text stays text. Errors go through
openai.py's `http_error`.

### The model queue

Every chat/completion call goes through one scheduler (`backends/queue.py`), reached
through a `QueuedBackend` (`backends/llm.py`) bound per call with a task and a priority:
runner streams (Chat), titles and document descriptions
(Titles and descriptions), photo descriptions. Embeddings and the reranker pick their model
through the same Router (below) but do not queue here.

- **Router.** `backends/router.py` knows each provider's state: reachable or not (and
  why, the server's or the connection's sentence), which models are loaded and which are
  starting (llama-swap `/running`, Ollama `/api/ps`, else `/models`), as it last learned
  them.
  `order(task)` gives the task's models that can answer now, in order, and a sentence for
  each one left out: no key yet, provider unreachable, could not be loaded (left out
  until the set of loaded models on that server changes, Check provider, or a change of
  the model's settings). A cloud model is an
  entry like any other: a model in a role's list is the user's pick, fallback included. Every change goes out once as
  `models.state` on the global feed and is logged once.
- **Health from requests.** What a server is like comes from the requests the app
  sends anyway: a request that lands says up; a refused connection, a timeout or a 502
  (a proxy whose model server is down) says down; a busy answer or an error is the
  model's state, in the server's words (`Router.problem`). A server is asked directly
  (its own cheap GET, 2 s timeout) only:
  1. once each at startup (`check_all`);
  2. on Check provider, and once when a new provider is saved or a provider's key is
     stored (`saves.provider_key`);
  3. once after a request landed on a model the server did not list as loaded (a swap),
     and once after a GPU lease unloaded its models;
  4. when a request got no event for 180 s on a server that lists what it loads
     (`_first_event`): once, strict; while it shows the model starting, and once when it
     shows it loaded, again after the next 180 s;
  5. once more, fresh (`max_age=0`), before a call the user waits on fails because its
     provider is seen down (`asked_again`: the state may be stale);
  6. when work needs a server known to be failing, at most every 10 s (`RETRY_DOWN`):
     a call's `order`; a work item through `revisit` (a deferred one among them); a
     waiting run through `can_answer`; and while it is paused, `revisit` only while a
     turn of its own is held.
  Nothing polls, and no time of how long anything was down is kept. A probe that times out keeps a server seen up as up (a busy box lists its
  models late); a strict probe or a failed request marks it down. A server's model list
  (context, caps) is read when it first answers and on Check provider. The state is in
  `GET /api/status` (`hosts`) and the metrics `celine_host_up` and
  `celine_host_down_total`.
- **Paused.** While every model of the chat and tasks roles is on a provider seen
  down, it is paused: the dispatcher claims no turn (`hold.agent_hold`; frozen wins
  over paused), routines and messages still queue theirs, and it resumes by itself.
  The state is `hold` in `GET /api/status`, the global event `agent.hold` and the
  metric `celine_paused`.

- **Slots.** One slot = one request in flight on one model: its `slots` in Settings, else
  one. Optional cap per provider (`max_concurrent`). A llama-swap provider runs one model
  at a time.
- **Requests per hour.** A provider's Requests per hour (`max_per_hour`, 0, no limit, by
  default) counts the requests to that provider (by its id) in the last hour, each as it
  is about to wait for its slot (`Router.take`, keyed by the provider; the reranker,
  embeddings and speech count too). The count is in memory, since the app started. Over
  it, a call the user waits on fails with code `busy` and "<provider> reached its <n>
  requests this hour."; a call with a fallback passes over that provider to the next
  model of its list while the provider's Next model at the hourly limit (`next_when_full`, on by
  default) is on; any other waits, holding no slot, until the hour has room; the box's
  model proxy answers 429 with the same sentence and a Retry-After. Models, a waiting
  run's reason and `models.state` show the sentence while it holds.
- **Order.** Strict priority: `interactive` (the user's turn) > `agent` (triggers, jobs,
  sub-agents) > `background` (titles, descriptions, photos). Round-robin between
  conversations inside a priority; on a llama-swap provider a call for the model that ran
  last goes first (`queue.AFFINITY`). A ticket that cannot start blocks its model for
  every ticket behind it.

- **Holding.** A slot is held from the request until `Done` (freed before `Done` is
  handed on) or until the reader stops (`aclose`); never during tool execution.
- **Your turn: one model.** A turn the user waits on (`fallback=False` on `LLM.bind`) runs
  on exactly the first model of its list. When that model fails, the turn fails with its
  error; no other model is tried. Only background work walks its list. A turn on a model
  that is not its list's first gets one tail line (`prompt.fallback_line`): "You are
  running in fallback mode. Instead of <first> you are running on <current>.", what the
  app knows of <current> (its context window, whether it sees images and calls tools;
  an unknown fact is left out), then why each model before it is left out.
- **Failure.** A failure before the first token (a request every model would refuse, too long or with an image it cannot take, fails the call as it is) marks the model and moves to the next one;
  after the first token, never. When every model failed with an error, the call fails with
  the last one; when they were unreachable, the call waits and tries whatever the Router
  offers next. A request that gets no event for `FIRST_EVENT` (180 s) fails with
  "<model> produced nothing for 180 s." like any other error: a server that sends
  keepalives or empty chunks never trips the HTTP read timeout. A request for a model
  its server did not list as loaded says "loading" once; at 180 s with no event that
  server is asked once, strict: while it shows the model starting, and once when it
  shows it loaded, the 180 s start again, so a cold load is not cut. A busy provider (status 429, 503 or 529; never a sentence in the body)
  never moves to the next model. A call the user waits on fails with code `busy`, and
  so does a model that could not be loaded: the chat offers Switch provider (the
  conversation's model chooser, then Regenerate) and Keep waiting (Continue with
  `wait`). Every other call, and a Keep waiting turn with every model call its tools
  make (bound with its turn id), emits `waiting` with reason `busy` ("Waiting for
  <model>. <the provider's words>"), waits and asks the same model again until Stop. At
  interactive priority (Keep waiting) it waits for the answer's Retry-After (at most
  120 s; 10 s without one); at agent and background priority it backs off: Busy model
  wait (`busy_retry_seconds`, 10 s) doubled at each try, Longest busy model wait
  (`busy_retry_max_seconds`, 600 s) at most, never shorter than the Retry-After
  (`backends/llm.py backoff`). The wait counts neither against the turn's
  clock nor against the time limit of the tool that waits. A 402 or an out-of-credits body fails the turn with code
  `out_of_credits`.

- **Images.** Images never change the order. Each request is fitted to the model that
  answers it, before its slot is taken (attachments.Images, below).
- **The GPU.** A lease (`gpu_lease`, an exclusive ticket) takes a llama-swap endpoint
  whole for a batch of images or music. Text goes first: a lease waits while any request
  runs there, then `gpu_idle_seconds` more. One lease per batch.
- **No model.** When no model of the task can answer, a turn waits with a `waiting` event
  (reason `no_model`), rechecks on every change and every 5 s. Stop always works. A
  wait under `MODEL_WAIT_FRESH` (300 s) goes on with the request as built; a longer
  one ends the call with a retryable error ("The model is back, but the turn's request
  is stale."): the request carries the clock line of the turn's start,
  and the turn ends failed and retryable, so Regenerate builds a new one. A Routine's
  run never waits here: its turn runs on exactly its first model and fails at once,
  and the run waits in the scheduler (Schedules, When a run fails).
  Background work raises `NoModel`; its work item is deferred.
- **Stop / timeout.** A stopped turn's waiting request leaves the queue at once
  (`QueueCancelled`); the turn ends `aborted`. Background work gives up after
  `background_wait_seconds` with a retryable error. Waiting time is credited to the
  turn's clock.

- **Visible.** Each call appends durable `llm.queue` events to the conversation
  (`waiting`, `running`, `left`); the global feed gets a `llm.queue` snapshot on every
  change; `GET /api/llm-queue` returns the same (`Queue.snapshot`, `Ticket.public`).

### The work queue

Background model jobs that nobody waits on are rows of `work` (`work.py`): `title`,
`appraisal` (its mood after a chat turn, `skills/mind.py`), `documents.describe` and
`bastet.answer` (Bastet's reply on the Intercom, `guard/talk.py`, on the guard role).
Image captions and the topic search index keep their own schedulers.

- **Shape.** An item: kind, priority, state (queued, running, done, deferred, failed),
  payload, not_before, a lease (worker, until), error, conversation_id. A worker
  makes five calls: `claim(worker, kinds)` (under a 120 s lease), `heartbeat`, `finish`,
  `defer(why, until)` and `fail(error)`; `put_down` at a stop.
- **The pool.** Two workers in the app (`work.Pool`). A server of an item's roles known
  to be down is asked again first (`revisit`); when every model the kind asks for is
  still on a provider seen down, it is deferred without a call; a
  `NoModel` or unreachable error defers it for 60 s. Any other error fails it with its
  reason: one try. Done and failed rows are deleted after 7 days.
- **Admission stays** with the model queue; the work queue only decides what is due.

### House and body

The seam for its own machine. The house holds its memory (database, notebook, archive,
documents, vault), the guard, the model gateway and the work queue; only the house reads
and writes the database. The agent's body runs its turns and its own tools: the shell,
the coding agent (`code`) and the browser. Today both are this one process.

### Abort

`POST /stop` is `Dispatcher.abort_conversation`: it sets `turns.abort_requested`,
cancels the turn's `Token`, cancels every job of the conversation and of its sub-agents'
conversations. A mission's Stop and `sub_agents__stop` use it too. Messages that came
meanwhile land by the delivery rule once the turn has ended. The streaming loop checks
the token per delta and closes the upstream request. Partial text is kept; the turn ends
`aborted`. A blocking tool that finishes during the stop has its output kept in
`tool_calls.withheld_output` and is reported to the model as `cancelled`.

### The tool chokepoint

`turns/tools._resolve` always returns a result. Its steps, in order: stopped, replayed,
frozen, offered, valid, Bastet, the call. A call's
arguments are parsed once (`run_tool_calls`). Paths: frozen (state `gated`, in any mode
of the guard), unknown tool, not in this request's list (`gated`), a skill that is down,
off (`gating.off_sentence` by `gating.reason`), arguments not JSON, arguments the wrong
shape (`tools.mistyped`, the call fails with the reason; before it, `tools._lenient`
reads a plain value written as a string as the type the schema asks for: "true" or
"false" for a boolean, "10" for a number, and converts nothing else),
raised, timed out, cancelled, cancelled but finished, journal replay, detached, not run.
`turns.finish_tool_call` writes the `role=tool` row for every one of them.

Every call it makes runs, a repeated one too. Not run: a call after the turn's work time
is up (`display.time_up`).

### Bastet (the guard)

`server/celine/guard/`. The module map and the entry point for one call
(`guard/calls.py`: `frozen`, then `at_call`, which asks `guard.closed` and `guard.on_call`)
are in `guard/__init__.py`; this section is the contract with the rest of the app.

Bastet's character is the user's to edit on Settings, Guard (`guard/persona.py`: Bastet's Persona, Replies to {name} on the Intercom and Messages to you, kv `bastet:<field>` with History, the shipped `config/guard/bastet.md`, `talk.md`, `note.md` the defaults; `GET /api/guard/texts`, `PUT /api/guard/texts/{field}`); Bastet's Intercom address is `system:guard`. In `_resolve`, after
the argument check and before the time check (`guard/calls.at_call`), Bastet answers the rule set on the call. The
one switch is `guard/switch.py`, asked through the port in `guard/__init__.py`: `off`
while the Guard feature is off (Settings, Features) or in a chat that turned the guard
off for itself (`conversations.guard_off`): nothing runs, no gate, no patrol, no answer
on the intercom. While the feature is on, the General setting `guard` (Guard mode on
Settings, Guard) is `watch` (Warn: the call runs at once, the rules are answered in the
background and only write rows; a closed gate still holds) or `on` (Enforce).

- **Rules** (`var/settings/guard.toml`, `guard/rules.py`, read at call time): each has
  `on` and `does` (`watch` writes a row only, `stop` does not run the call, `freeze` stops
  and freezes). A rule is asked only where code found a hard fact
  (`guard/facts.py`), and a finding needs the judge's yes with a quote code finds in the
  evidence (`verdict.confirm`). Each rule's words and which parts of Bastet take it are
  one row of `rules.TABLE`. The rules: `escape`, `hijack`, `leak`, `damage`, `confirm`, `sites`, `stops`,
  `persist`, `injection`.
- **Gates** (`[gates]` in guard.toml, `rules.gate`): per data class and sink, `open`,
  `judge` or `closed`. A closed gate is plain code (`guard.closed`), the same in Warn and
  Enforce: no model is asked and nothing opens it but the file.
- **The judge** (`guard/questions.py`): the guard role (Decision model) answers named
  questions with `{verdict, quote, why}`. Questions and frames are files (`config/guard/`).
  A second look by the Second look model (`[chain]` in guard.toml) is `guard/chain.py`.
  No judge asks for a JSON mode. An answer without a verdict in the letters it was asked
  for (`small_role.Unreadable`) never passes, in Warn or Enforce: the row keeps what the
  model wrote, and Enforce refuses the call ("Not run: Bastet could not read her decision
  model's answer."). A model error a retry cannot change (`small_role.Unanswerable`: the
  request does not fit, an input it does not take, no credits, a non-retryable status) fails
  the call with its words. No model now, a timeout or a busy server
  (`small_role.Unavailable`): Enforce holds the step, its line "Waiting for Bastet. {why}" once
  per new why, and reads the switch again before every ask; turned to Warn or off, the
  wait ends, the step runs and its rows are `unreachable`. No duration is shown.
- **Page check** (`guard/page.py`): every outside result is checked as it is received
  (`turns/tools.py` `checked`); a hit is a mark for the hijack check and never blocks
  reading.
- **Patrol** (`guard/patrol.py`): at random intervals Bastet looks back at finished turns
  and reports findings on the Intercom.
- **What the agent reads**: every stop, freeze, confirm and pass is one line from Bastet in
  the tool result (`bastet.py`), in plain words and never a rule name, threshold or
  score. An Allow once or an Allow for 7 days on a stop's banner is Bastet's line on the Intercom that
  wakes the agent (`talk.to_agent`), so it does the step again now. The agent's chat
  (`guard/talk.py`) is `intercom__send(to="bastet")`, answered by the `bastet.answer` work
  item; its reply is sent as the model wrote it, and it never grants anything.
- **Rows**: `guard_decisions`, one per rule that had a reason. A check stopped while the
  judge works fails its row with why `stopped`; a restart fails what the last run left
  waiting (`store.close_asked`). A stop the second look overruled counts as none: not
  for the stops rule, not as a stop persist tries again, not as a message to you already sent.
- **Freeze** (`guard/freeze.py`, kv `frozen`): every live turn ends `aborted` with
  `reason_code` `frozen`, every job is stopped, no worker claims a turn (`hold.agent_hold`)
  or work item, no tool runs, the model proxy answers its shellbox 403, and a named token
  only reads the intercom. Global event `frozen.changed`.
- **Passes** (`guard/passes.py`, `guard_passes`): a rule (or `all`) in one conversation
  (or everywhere) until a time; with a `piece` (Allow for 7 days on a banner), only
  calls whose evidence for that rule is that exact piece. A pass on the rule that stopped
  something covers a try again at it (persist). Global event `guard.passes`.
- **Banners** (`guard/banners.py`, `guard/view.py`): what acted in mode On shows in its
  conversation as the `guard` list of the snapshot; every change is the conversation
  event `guard.banner`. A stop or a freeze carries its `actions` (`once`, and `pass` when
  it has a piece) and the `help` of its "?".

**Routes** (logged-in session only; a request with a token or key gets 403
`passkey_only`):
- `GET /api/guard/decisions` (filters `decision`, `rule`, `mode`, `source`, `turn`,
  `limit`, `before`), `GET /api/guard/decisions/{id}`
- `GET /api/guard/passes` (each with its rule's `label` and a `line`), `DELETE
  /api/guard/passes/{id}`
- `GET /api/guard/rules` (the rule set, its `rows`, `acts` and the page's `help`), `PUT
  /api/guard/rules` (the tables to change)
- `GET /api/guard/freeze`, `POST /api/guard/freeze` `{why}`, `DELETE /api/guard/freeze`
- `POST /api/guard/banners/{id}/replies` `{text}` or `{button: approve | deny | unfreeze | once |
  pass}`; 503 `bastet_away` when the reader cannot answer
- `POST /api/approvals/{id}/answer` `{ok}` → `{"ok": true}` (`api/approvals.py`; the
  cards are listed by `GET /api/waits`); every request but a gate also waits in its chat, after the message that made the call, with Approve and Deny
  (snapshot `approvals`, events `approval.asked`, `approval.closed`). Inside any turn the
  call waits for your answer or Stop, and the turn's wall clock and the tool's time do
  not run meanwhile: a chat or a mission waits until you answer (a mission's time limit
  counts the wait off), a routine's run ends at its own time limit (reason
  `ASK_LAPSED`). Only a request outside a turn waits `ASK_FOR` seconds (120).
- `GET /api/waits` `{items, count}` (`api/waits.py`, `waits.py`): everything that waits
  for you (Waiting for you)

**What code enforces** (`tests/test_gates_hold.py`): the floor under Bastet's judgement.
- A closed gate never opens.
- Frozen, the house leases nothing and runs nothing new: no turn, no work item, no tool,
  no model call through the proxy, no action with a named token.
- Only you unfreeze, make a pass and answer an approval or a banner: those functions take
  an `Owner` (`guard/owner.py`), made only from a logged-in session (`guard/api.py`,
  `api/approvals.py`). No tool, model, intercom message or Bastet's chat holds one.
- The agent's tools cannot reach the rule file, the database, the decisions, passes or freezes:
  file tools resolve every path inside their root, and the shellbox and the browser see
  only the shellbox folder of the data volume.
- One chokepoint: a tool's function runs only in `turns/tools.py` and the jobs it hands a
  step to. Sub-agents, routines, missions and MCP tools are turns or tools like its own.

### Durable execution

A tool declares `@tool(mode=...)`:

- `blocking` (default): the turn waits up to `tool_timeout_seconds`.
- `detach`: the step is started once in the job nursery and the turn waits for it,
  up to the tool's timeout or the turn's time left, and stops it then; nothing is put
  in the background unasked. A call it asked into the background (`shell__run`
  `background=true`) is promoted to a job after 5 s: the model gets `running in the
  background as job X`, the turn carries on, and the result comes back later as an
  intercom `job` message (`intercom/deliver.py`): at once with a queued turn when the
  conversation is idle, else at the live turn's next step.

The context (`turns/context.py`, `_Item.wire`) rebuilds the request from rows alone: an assistant message with
`tool_calls` rows carries them as `tool_calls`, each `role=tool` row carries its
`tool_call_id`, and a call whose result row is missing gets `result unavailable`, so a
turn continued after a restart sends the same shape the running loop does.

Ownership is handed over synchronously so exactly one side settles each step. A job
always has `deadline_at`. Stop reaches a handed-off step through
`jobs.cancel_for_conversation`; when a turn ends for any reason, `jobs.cancel_for_turn`
stops the steps it still owned.

A job ends once, as one intercom `job` message to its conversation (`jobs/store.finish`,
one write: the call gets its verdict and `display`, the job row its state and result).
The tool row it read while it ran is never rewritten, so a request built later sends the
same history and the prompt cache holds. The message wakes it unless it is quiet. A
cancelled job records `stopped_by` (user, turn). A job the restart ended is
`interrupted`, and the restart's notice names it. A job and its conversation cannot part
ways: deleting the conversation, or the reply that holds the call, is refused (`conversations/tree.Busy`)
while a job of it runs.

A job is one shape wherever the web sees it (`GET /jobs`, `GET /jobs/{id}`): the `jobs`
row plus the call it ran, joined from `tool_calls` (`jobs/store.py`): `tool_call_id`,
`message_id`, `started_at`, `display` (null while it runs) and `progress`, the latest
`tool.progress` line. A tool's `ctx.emit` goes through `turns.tool_progress`, which
appends the event and writes the same line on a running job's row in one transaction.
Tool calls in the snapshot carry `started_at`, `finished_at` and `job_id`.

At startup `turns.sweep` ends each turn a crash left running `interrupted` (see Shutdown and restart).
Running jobs and their `detached` calls end `interrupted`; they do not survive a restart.

## Skills

`@tool(mode=, read_only=, destructive=, open_world=)` on a coroutine. The
three annotations are MCP's tool annotations (`readOnlyHint`, …) with MCP's defaults for a
tool that writes (destructive); `ToolSpec.mutating` (not read-only) is
what the runner uses: only read-only blocking calls run again after a restart, and the
calls of one reply run concurrently when each is read-only and blocking or hands
non-destructive work to a background job. MCP servers' own annotations are mapped the same way (untrusted hints). JSON
Schema is derived from the signature: `Annotated[T, "text"]` is the description,
`Literal` is an enum, `X | None` and defaults are optional, `ToolContext` is stripped,
`additionalProperties` is false. Wire name is `skill__tool`.

The docstring is the model-facing description: what it does, when to use it and when
not (naming the other tool), then `Returns:`. Active sentences, at most 25 words each. A
tool name is `<what it is>__<verb>`, and each verb has one meaning; `tests/test_verbs.py`
holds the table and checks every built-in tool against it. Labels and summaries for the
UI live apart (`settings/tools_meta.py`). Descriptions and schemas have `{{username}}`
and `{{name}}` filled in once, when a skill registers (`Registry.add_specs`).

**The agent's** (`celine/agent.py`). The app has one agent, the one each user creates and
names (a new install prefills Celine, she), and its identity is the one row of `agent`:
name, pronouns, colour, portrait, persona, talk, voice (its Banned words,
`settings/voice.py`), `updated_at` and `named`. `cfg.agent` is the live row, read by every
`boot.load` and again through the Store after every write (`agent.refresh`); agent.py opens
no connection. The shipped base is `config/agent/`; a fresh install seeds the row from it
(`agent.seed`). Its name is `{{name}}` in every text and prompt; a prompt that tells a model
its pronouns uses `{{pronouns}}` (she/her, he/him, they/them or it/its), and its own
system prompt says them in one line. Settings, the agent's page edits every field (`GET`/`PUT /api/agent`; the global event
`agent.changed` follows every write); the editors keep a History (`texts.KINDS`: `agent`,
`bastet`, `routine`, `mission`, `lyrics`, `prompt`).

**Migrations run once, at startup** (`__main__.preflight`, and the export and purge
commands; `__main__.migrated`): boot.load never migrates, so a Settings save or the file
watcher never touches the database. A database the build cannot bring to its schema ends
the start with one line: what failed and the copy from before the update
(`db/migrate.py` `Failed`).

**Kinds.** What a conversation is is its kind (`conversations.kind`, fixed at creation):
`chat` (the user's), `mission` (`POST /api/missions`), `agent` (a sub-agent),
`background` (a fired trigger or a run of a schedule, hidden from the chat list). How
it talks belongs to a chat, which runs on the chat role; a conversation of another kind
runs on the `tasks` role (a run on its schedule's role, `settings/roles.py`) and gets its
instructions from its kind, never how it talks.

**What it carries** (`turns/loads.py` `select`, at every turn start and for /usage,
/compact and the Context tab): the notes it always carries and the skills offered here.
- Notes: a note whose head has `load: always` and the notes on the user,
  `people/{{username}}*`. One over 1.5 KB (`loads.ALWAYS_MAX`) carries its head lines and
  the lines it marked with `!!`, then `rest via notebook__read(path="...")`. Never: a
  note whose head says `load: off`, a note the user left out here
  (`conversations.notes_off`), anything outside a sub-agent's folder. Any other note
  comes back as the result of `notebook__read`, which ages like any other result
  (`compact.stub`); a note whose `keys:` head line comes up in the talk brings its
  matching sections under `[your memory]`.
- Skills: every skill offered here with a tool on.

**The system prompt** holds nothing that changes from turn to turn, so the start of
every request is the bytes of the one before: the persona, the legend of the app's
marks (`turns/legend.py`), the line about who it is talking to (`_chat_line`), **where
it is** (`conversations/here.py` `where`, from ids: its kind and address, the place, the
device its tools run on, its model role, who started it, who it reports to and its depth
in the chain of agents), its `talk`, the **notes block** (`skills/notebook.py`
`notes_block`), the **skill index** (`Registry.index`: one line per skill offered here,
the core ones marked `[core]`), then each core skill's fragment (`prompt_fragment`: what
holds for the whole conversation, such as a mission's Instructions or the notebook's
`identity.md`) and the sub_agents
fragment.

**The tail** of a turn (`prompt.tail_text`): the date and the time now, the fallback
line, the other conversations working now (`here.busy`), its plan, the skills it
cannot use now (`prompt.unusable`) and the ones it keeps notes on, and each core skill's
live state (`tail_part`: the intercom's thread, announcements, scheduled messages and
sub-agents, a mission's time limit, status and delivery, a sub-agent's report room, the
notebook's index). It is written once on the user
message the turn answers (`message_context.tail`, `message_context.put_tail`) and sent
after that message's context block in every later request (`context._Item`), so it never
moves; a later turn on the same message (a Continue, a restart) sends the same bytes.

**The tools.** `prompt.tool_specs`: the tools the skills offer here, in registry order
(a skill with `offered_in` offers its tools only where that says so); a sub-agent with
read access keeps the read-only ones and the intercom. `prompt.wire_tools` sends every
one of them that is on, a core skill's (`skills.CORE`) with its parameters
(`ToolSpec.as_wire`), every other by name and the first sentence of its description with
open parameters (`ToolSpec.as_name`; a tool that takes no parameters goes in full). A
load never changes the list. A tool that is off (`gating.is_enabled`) or of a skill that
is down is not sent; the skill index says so, and a call to it is refused with why. A
call to a tool sent by name runs when its arguments fit its real schema
(`turns/tools.py _invalid`); when they do not, the error carries the tool's full
description and parameters.

**Loads append.** `skills__load(names)` (skill `skills`, `skills/meta.py`) answers with
the parameters of the tools named, or of every tool of a skill named, with that skill's
guide, its fragment (a core skill's is in the system prompt already) and its note
(`skills/<name>.md`), as its result: the system prompt and the tools stay the same bytes,
so the model server's prompt cache holds, and the result ages like any other. A skill
whose parameters take more than one result holds says so and names a tool to load by
name. A skill loads when it calls `skills__load`, or with a call of one of its tools
while its guide is not in what it reads (`meta.had`: no `skills__read_guide`, load or
such call on the active path after the newest summary and the aging mark, read once per
turn): that call's result brings the same load after it, under `[loaded with this
call]`, outside any wrapper, in the room the result left of the cap (display `guide`,
`tools._with_guide`, `guard/mark.shown`; a replay keeps it; the client never gets its
text, `mark.for_client`). A guide aged or summarised away comes again; a sub-agent's
conversation has its own. A core skill's calls bring nothing.

**Guides.** Each built-in skill has a guide, `server/celine/skills/guides/<skill>.md` (in the package, read once), SKILL.md style:
frontmatter `name` and `description` (one line: what it is and when to use it), then the
body (how its tools go together, examples). `skills__read_guide(name)` returns the guide
of a skill offered here and its notes on it (`skills/<name>.md` in its notebook);
`name="skills"` returns the skill index.

`sub_agents` (`sub_agents/`): `start(brief, name, access, folder, model, cli, device,
template)` starts a sub-agent, a conversation of kind `agent` on the tasks role, with
`agent_of`, `agent_name` (unique among the parent's sub-agents), `notebook_folder` and
`access` (read or work; read by default) and its Instructions as an intercom `brief`. The
name of a sub-agent that is done sends the new Instructions to it instead. At most
`sub_agents.max_running` of a parent's sub-agents work at once; the rest are queued, and
`release` starts them oldest Instructions first. With `cli`
(`agent_cli`) its turns run on `sub_agents/cli.py`'s backend in its shellbox instead of a
model, calling its models through the model proxy (`api/llm_proxy.py`). With `device`
(one of its devices, such as mac) opencode works on the Instructions there, on its model
(`sub_agents/drone.py`). `template` names an agent template in `templates/` of its
notebook: its prompt, tools, access, model and device, the arguments given win. A sub-agent
starts its own, down to `MAX_DEPTH` (3) levels below the top conversation
(`conversations.store.depth` reads the `agent_of` chain); the deepest is not offered
the skill. A `model` on a cloud provider (`agent_models.cloud`: the provider's Cloud switch,
else its address is not on this network: not a global address, so Tailscale and CGNAT
count as local, or a single label or .local, .lan, .home.arpa, .internal) that the user
did not pick (`agent_models.picked_by_user`: not the conversation's own, not set by the
user on a step of its plan, and in no role's list, or in one while Ask before cloud models
from your lists, general `ask_listed_cloud`, is on) waits for the user's approval (kind
`model`, `agent_models.ask_cloud`) at every start and resume; so do models__ask,
code__start, missions__start and a plan step's model, and `models__list` says which ask
first. A code__start on a model the box is served already (`granted`) asks nothing. A
cloud model provider fills no empty role on its own (`views.fill_roles`; ComfyUI, with
no Cloud switch, fills its preset roles). The box's model proxy serves a cloud model
only to approved work: one in a role's list, a live CLI sub-agent's, or one code__start
was approved on. That approval is a row of the table `model_grants` (migration 0025,
`agent_models.grant_sync`, `keep_sync`, `granted`): it lasts `BOX_GRANT` (600 s) after the
approval or the model's last call through the proxy, and survives a restart. Any other
cloud model gets 403 with the way to ask: code__start, or sub_agents__start with `cli`
(on a device, sub_agents__start with that device). code__start's `POST /code/session`
on the shellbox restarts opencode first when the model is new to its list (a cloud model
just approved) and starts the session in the same request; while a session works, that
restart, or a start on a busy session, gets 409. A sub-agent is
done when its conversation is quiet and so are its own sub-agents (`report.quiet`);
`sub_agents/report.py` then sends one intercom `report` to `agent:<parent>`, never
further up: a head (name, state, models, tokens, seconds, notes changed) and its final
answer cut to `REPORT_SHARE` of the parent's window. What it opened closes
(`leftovers.py`). A sub-agent writes only to itself, its parent and its own sub-agents
(`addresses._reach`).
`read(name, wait)` looks at one, `stop(name)` stops its turn, jobs and scheduled
messages. Read access gives it only read-only tools and the intercom (`prompt.tool_specs`,
`turns/tools.py`). `GET /api/sub-agents` rows and `agent.state` events carry `access`.

`Registry.build` constructs a skill only when `[skill] enabled` is true globally.
`gating.is_enabled` resolves per conversation: exact tool override, then skill override,
then the global default. A skill override clears the tool overrides under it (the bulk
verb, as in Settings). Overrides carry a `client_seq`; an older one is ignored.

`notebook`: `write(path, content, summary, mode)`, `read(path, from_line)` (each line as
`<n>:<hash>| text`), `edit(path, edits, summary)` (hashline edits by `n:hash` anchors;
every anchor is checked before anything changes, a stale one refuses the call),
`search` (FTS5 over summary and body), `list`, `move`, `delete`. Files live under
`var/notebook/`, confined, any format; the index is `notebook_index` and `notebook_fts`
in `var/notebook/.index.db`.
Line 1 of every note is a `Last changed:` line the code keeps up to date. The index
goes into the tail of every turn (`tail_part`, `build_index_fragment`), never the system
prompt, since it changes as it writes: `map.md` first, up to `notebook.index_max_entries`
entries within `notebook.index_max_chars` (Settings, Advanced). `tools/` in the notebook browser is generated from the
registry on each read (`skills/toolsdoc.py`) and is not in its notebook.

Placeholders are created once (marker `.placeholders`): `appearance/*.md` and
`opinions/{{username}}.md`, all its own. Any other file can be protected from the browser's
right-click menu; those marks live in `kv` under `notebook_protected` and count like the
config list for its `write` and `delete`.

Every notebook tool speaks to it in the second person and says whose file is whose:
`list`, `search` and the index suffix your files with `[{{username}}'s, you can read it,
not change it]` and the generated folder with `[generated, read-only]`. Three files are
yours, `SEEDS` in `skills/notebook.py`: `identity.md`, `opinions/README.md`,
`appearance/README.md`. They are written once at startup from `config/notebook_seed/`
when missing, and its `write` and `delete` refuse them. `identity.md` goes into every
system prompt in full (the skill's `prompt_fragment`).

`shell` (`skills/shell.py`, guide `guides/shell.md`, on demand): its terminals in its
shellbox (`shellbox/shellboxd.py`, DEPLOY.md "Shellbox"), reached at the address in
Settings, Features (`settings/services.py`, `CELINE_SHELL_URL` until saved) with
`keys/shell.key`; without an address the skill is `down`. Seven tools, and the three
share tools (Shares, below):
- `run(command, terminal, timeout, background, about)` runs the command in the terminal's own
  shell through a function the daemon installs (`RUN_SH`), so directory and variables
  stay. Done is the `<id>.exit` file the daemon watches; the result carries exit status,
  duration, the log path and the output cut once. A command blocked reading the terminal
  comes back `waiting`; at the timeout `running`, and it keeps running. A run while
  something runs in front comes back `busy`. `background=true` turns the wait into a job.
  `about` is what the command does in a few words, the row the user sees.
- `wait(terminal, timeout, background)` waits for the end of the terminal's latest run as
  `run` waits for its own, or gives its result at once when it ended already.
- `type(text, keys, terminal, enter)` types into the program in front and answers with the
  screen once the terminal printed nothing for 1 s.
- `stop(terminal)` presses Ctrl-C; the next stop sends SIGTERM, then SIGKILL.
- `read(terminal, from)` pages the log of the terminal's latest run, or shows the screen;
  without a terminal it lists its terminals and yours.
- `send(path)` attaches up to 5 files of its home to the message; `look(path, question)`
  shows it up to 4 images of its home.

Files go through the daemon's `PUT /file?path=` and `GET /file?path=` (streamed; a path
leaving its home is refused). `box_files.py` is the app's side. Every file you upload is
copied in the background to `~/uploads/<sha12>/<name>` while the shellbox is on
(`<sha12>`: the start of its sha256; bytes already there are not sent again, so its edits
to that copy stay); a chat attachment keeps where its copy
landed (`attachments.box_path`) and its context names it. Every shell and browser call
names its terminal or tab; the conversation's own (`terminals/store.default_terminal`) is
named in the skills' prompt fragments. Your own terminals it reads always and runs, types or
stops in only while `may_type` is on. The skill upserts the terminal row from the result's
`display`, with `opened_by` the conversation whose call opened it.

What a conversation leaves open (`leftovers.py`, on every `Dispatcher.on_end` and after
`POST /missions/{id}/stop`): a mission done or stopped, or a quiet sub-agent, closes every
open terminal and tab it opened. Each sync round also closes its terminals with nothing
running whose `quiet_for` reached a day (`sync.IDLE`). Logs are never deleted: a raw log
past 64 MB and a run's log an hour after it ended are gzipped.

`browser` (`skills/browser.py`, guide `guides/browser.md`, on demand): its own Brave in the
browser box (`browser/browserd.py`, DEPLOY.md "Browser"), reached at the address in
Settings, Features (`CELINE_BROWSER_URL` until saved) with `keys/browser.key` through
`browser/sync.py`; without an address the skill is `down`.
`open(url, tab, profile)` loads a page in a named tab (four at most; `profile="throwaway"`
is a browser context of its own); `act(steps, tab, show)` runs its steps in order, each
`{do, target, text, ...}` (`Step`): it clicks a row, types, presses keys, goes back,
answers a dialog or restarts the browser; a `locate` step is resolved app
side by the Screen reading model (`vision.py`, role screen) and becomes an ordinary point
click or hover (`skills/browser.py _locating`). `scroll(tab, direction, to, amount, show)` scrolls the
page or brings a row into view, `close(tab)` closes a tab. `look(tab)` reads the page's text, and
`look(what="locate", ...)` answers with a box and clicks nothing. open and act answer with
the page result: tab, address, title, then the numbered rows it can click. The agent's calls on
one tab wait for each other. While you have taken the browser from its page (`/browser`),
every call of its own gets 409 `taken`. The agent's tabs are rows (`browser/tabs.py`, table `tabs`):
`sync.watch` opens a row for a new tab name with `opened_by` the conversation of the newest
browser call naming it, and a tab unused for a day (`tabs.IDLE`) closes. `GET /api/browser`
gives each tab its `conversation`.

`artifacts` (`skills/artifacts.py`, guide `guides/artifacts.md`, on demand): its pages and
documents for you (`artifacts/`). `write(title, content, id, note, kind)` makes an artifact
with version 1, or with an id adds the next version with the whole content; `kind` is `html`
or `markdown`. Versions only grow; you restore an old one as a new version. The result names
the id and the chip `[[artifact:<id>]]`, and `hint:` lines from `artifacts/lint.py`. The
page is served by `artifacts/page.py` with its own CSP and `sandbox allow-scripts
allow-forms`. Every write appends `artifact.updated` `{id, n}`. `list(where)` and
`read(id, version?, part?)` complete it (`part`: one part of a long version, from 1).

Shares (`shares.py`, the `shell` skill's `share(title, port | path, public, hours, device)`,
`unshare(id)`, `list_shares()`): a server it runs in its box, or a file or folder of its
home (or of one of its devices, `device`), opened by you at `/share/<token>/` (table `shares`; the 24-character token is its id).
A port goes through shellboxd's `GET /forward` (a raw tunnel to `localhost:<port>` in its
box), HTTP and WebSocket, streamed; a path through `GET /file` with its content type and
byte ranges. A share is private unless it made it public: `/share/` passes the session gate
and the origin check, so the card opens `GET /api/shares/<token>/open` behind the login,
which sets the share's cookie (`share`, HttpOnly, SameSite=Lax, path `/share/<token>/`, until
its end; Secure with an https public origin) and redirects there; a page load (a GET with
`Sec-Fetch-Dest: document`, or none) of a private share without that cookie is 404, and so
is everything after its end. The agent's own browser, at the app's service name, needs no cookie.
The agent's page's own requests come from its opaque origin, which sends no cookie with a module
script or a fetch, so only a page load asks for it. `/share/` drops the Cookie header and its
Set-Cookie, and every answer carries `Content-Security-Policy: sandbox ...` (no
`allow-same-origin`) and `Access-Control-Allow-Origin: *`. Every share has an end time
(`ends_at`: 24 hours by default, at most 168; public 2, at most 24); the end is passive, no
job and no event: the proxy and the lists compare it with the clock. A share also ends with
`unshare`, your Stop (`POST /api/shares/<token>/stop`), or a reset of its box (the box's
`X-Shellbox-Reset` differs). The agent's shares, their Stop and its ends publish `share.updated`
`{token}`; `list_shares` names the ones that ended in the last day. Shares are a feature
of their own (`shares`, on only once saved on, after the shellbox): off, the three tools are
not built (the catalog's `tools`) and `/share/` and the open route answer 404 for every
share, even one that still runs, which shows as not running.

`intercom` (`intercom/skill.py`, guide `guides/intercom.md`, always loaded): its side of the
intercom (see Intercom). `send(text, to, subject, reply_to, in, at, until, wait)`
writes to `user`, `me`, `parent`, `team`, `all`, a sub-agent's or mission's name or a
conversation id; `in` or `at` make it land later (to `me`, a reminder that starts its turn);
`until` on a message to `all` makes it an announcement. `read(thread, unread, kind,
with, limit)` lists its threads or reads one; `with="user"` lists every thread between
the user and any of its conversations, each with its home (`store.source_of`: kind,
title, conversation id) and whether it waits for the user; `kind="job"` lists the
`jobs` rows of every conversation, running first, each with its result message's number;
`cancel(id)` cancels a message it scheduled. Sub-agents in read mode have these tools
too.

**Missions** (`missions/skill.py`, `missions/store.py`, guide always loaded): a mission is
your Instructions in a conversation of its own, started from the UI (`missions/api.py`) as an
intercom `brief` from `user`. The skill's prompt fragment puts the Instructions, where it reports,
the time limit and the status into the system prompt on every turn of that conversation, so
compaction never loses them. The time limit is a `notice` from `system:app` due before the
limit (`missions/store.book_time_limit`). `update(status, note, artifact?, force)` records
the status `active`, `needs_you` (Waiting for you) or `done`, and what it delivered
(`artifact:<id>` or `documents:<path>`; `force` closes it before its minimum time);
`mission.state` carries `artifact`. A stopped mission takes nothing new. `list(status)`
(each with the first line of its brief) and `read(mission)` are offered everywhere
(`offered_in` gives the rest only in the mission's own conversation): `read` finds one by
name or conversation id (`store.find_sync`) and gives its status, brief, report
conversation, limits, artifact, plan steps, sub-agents with their latest report
(`sub_agents/skill.agents_of`) and its newest intercom messages to and from the user.

**Plans** (`plans/`, core): one plan per conversation, table `plans`: `text`, `steps` [{title,
state todo|doing|done, checkpoint none|ask, model, by}], `waiting`; `by` is who set the
step's model ("user" on the Missions page, "agent" with plan__set, None without a model).
A cloud model the user set on a step never asks (`agent_models.picked_by_user`). The rules are
`plans/store.py`'s only: at most one step doing; done starts the next open step, except done
on an `ask` step, which sets `waiting` and starts nothing. `set(steps, text)` writes it
whole, `step(n, state)` moves one. Every change appends `plan.state` {plan} to the
conversation and, for a mission, `mission.state`. A step with a model switches the
conversation to it when it becomes doing (`flow.follow_model`). The user speaking there
clears `waiting` (`answered_sync`). The agent's request carries the plan in its tail every turn
(`flow.block`). The runner's continue rule (`turns/runner.py`): a step whose calls are all
`needs_answer=False` (notebook and mind writes, `skills.ToolSpec`) with answer text ends the turn only when no step is open or
it waits; a `plan__step` that reached an ask checkpoint ends the turn at once.

`routines` (`schedules/skill.py`): `list(night, runs)`, `read(name)`, `write(name,
label, prompt, time|cron, role, on, note, after, wall_minutes)` and `run(name)`. `list`
gives the routines and the triggers (`triggers.declared`, its on or off, `triggers.fired`,
its `trigger:<name>` conversation); with `night` or `runs` it gives runs instead, every
routine's: `run:<id>`, time, state, error or reason and the result's last line, as
`read` gives a routine's newest. A write goes through
`schedules/edit.save`, the same path and checks as PUT /schedules/{name}; a run it starts
has `runs.started_by` `agent`.

Where it works (`conversations/here.py`): its system prompt says the conversation it is
in (chat, mission, a Routine's run, sub-agent or background task) with its intercom address;
the tail of a turn names the other conversations with a live turn.

`past_conversations` (`skills/conversations.py`): `search(query, limit)`, FTS5 over user
and assistant messages on every conversation's active path, the current conversation left
out; `list(kind, limit)`, the newest conversations by `updated_at`; `list_day(date,
kind)`, an index of the conversations with messages on one day (`kind`, `KIND_SQL`:
chat, mission, run, task, sub-agent); `read(conversation, start, end,
tools)`, one conversation's active path in slices, from the end when `start` is left out,
the conversation by id, `run:<id>`, a mission's name or a trigger's name;
`read_tool_call(id, part)`, one earlier tool call whole.

`web` (`skills/web.py`, guide `guides/web.md`, its knobs `web.*`): `search(query,
time_range, language, limit, page)` asks a self-hosted SearXNG (the address in
Settings, Features, `CELINE_SEARXNG_URL` until a saved one) and returns
compact `[web:n]` blocks. Past Searches per hour (`web.per_hour`, 0, no limit, by
default: the searches of the last hour, counted in memory since the app started) a
search is refused with "Web search reached its <n> searches this hour." before SearXNG
is asked. `read_page(url, part, save)` takes a URL or `web:n`, fetches it (httpx,
`web.timeout` 20 s, `web.page_max_mb` 5 MB HTML, `web.pdf_max_mb` 25 MB PDF, redirects
followed by hand), extracts the main text (trafilatura, pypdf) and returns one part of
about `web.read_tokens`; with `save` it attaches the file itself to the conversation.
Only http(s) to global addresses is fetched: every hop's host is resolved and each address
must be global (`ipaddress.is_global`), and the peer address is checked again before the
body is read. Citations: it writes `[[web:n]]`; the runner's citation check
(`web.normalize`, `web.problems`, `web.link`) flags every n no web result in its context
showed (marked `|invalid`, as archive refs are) and stores the good ones as Markdown links.
Queries and fetched URLs leave the machine.

`email` (`skills/mail.py`, guide `guides/email.md`, on demand): one email account over
IMAP, set in Settings, Email (`settings/mail.py`: `var/settings/email.toml` and the
write-only `var/secrets/email.key`). Each call opens one connection in a worker thread
(stdlib `imaplib`), works and logs out. Listing, searching and attachments use EXAMINE and
`BODY.PEEK`, so no flag changes. Tools: `list`, `search`, `read`, `list_folders`, `mark`,
`move`, `delete` and `get_attachment`. A message id is `<folder>:<uid>`. Every failure is one
sentence (`MailError`).

## Knowledge bases

`config/kb.toml` has one block per knowledge base; `kind` picks the module. `KBs`
(kb/__init__.py) builds them at startup, in a thread, and a block that fails to open is
a config problem, not a dead server. Skills read the running set as `self.app.kbs`
(core/services.py), so skills rebuilt by a config reload find it. `[embeddings]` in the same file holds how
passages are embedded (dims, prefixes, input length, workers, refresh). The model is the
Topic search role in Settings, and whether anything embeds is that role alone
(`semantic.embeddings_available`; the reranker: `rerank.reranker_available`).

### Files

Each knowledge base is `var/kb/<id>.db`, WAL, `busy_timeout` 15 s. `KBFile.read`
keeps one connection per thread per file; `KBFile.write` opens a short-lived one.
There is no writer task: imports run in threads and commit in batches, so another
writer (a rename) waits at most one batch. sqlite-vec is loaded on every connection.

### kind = chat

Schema in `kb/migrations/chat/`: `kv`, `people`, `chats`, `messages` (+ `messages_fts`,
external content, unicode61 with diacritics removed, kept by triggers), `message_edits`,
`imports`, `ignored_chats`; the instant search index from `kb/instant.sql`; and the
semantic index tables from `kb/migrations/` (sessions, passages, int8 +
sign-bit vectors).

- `messages` is unique on `(chat_id, key)`, the source's own id. An import upserts: a
  new row is inserted; an existing one only gains a sender it lacked, an edit, a reply
  link or media info. Nothing is ever deleted by a write. "Added" is counted as rows
  above the id the write started at, so records seen again report 0 exactly.
- Senders are `people` rows. `names.resolve` picks the display name at read time:
  `custom_name` (yours), `contact_name`, `display_name`, `push_name`, `username`, the
  phone formatted with libphonenumber, then `Unknown 1234`. A raw JID or LID is never
  a name.
- `chats.message_count`, `first_ts`, `last_ts`, `last_message_id` are refreshed for the
  chats a write touched, when it closes.
- Paging is by `(ts, id)` row values, both directions and `around`.
- Search: every word must appear and the last is a prefix (`"a" "b"*`); input with FTS5
  syntax is passed through; a syntax error mid-typing returns nothing, not an error.
  Snippets use control-character sentinels, are HTML-escaped, then get `<mark>`.

### Task context

`kb/importers/`: the context every archive task shares (`ImportContext`: progress and
cancel; `ctx.check()` raises `Cancelled` between batches, what was written stays). The
app has no import of its own (no Import page, no upload route); archives fill from the
connectors, and the tests build theirs with `kb.sink()`.

### Connectors (Discord, WhatsApp)

The app never talks to Discord or WhatsApp; two connector processes own the accounts and
the app is their MCP client (it is never an MCP server).

- `connectors/discord/discord_link/` (Python, `python -m discord_link --state
  var/discord-link --listen 127.0.0.1:8768`): `state.py` (one SQLite file: the journal,
  dedupe, channels, cursors, spans), `rest.py` (GET only), `gateway.py` (live messages,
  edits, deletes, catch-up after a drop), `sync.py`, `spans.py` (the message id ranges the
  journal holds all of: a read tool answers those from the journal and asks Discord only
  for the gaps), `login.py`, `tools.py` (MCP read tools), `app.py`. It never writes to
  Discord.
- `wa-link/` (Go, whatsmeow): pairs as a linked device, journals history and live messages
  as the same records, serves the same doors, downloads media on request, asks the phone
  for older messages. On `LoggedOut` it exits and starts again with a new code.

App side:
- `kb/connectors.py`: `Connectors` (one per `[servers.*]` block with `kb` and an http
  `url`) polls `/admin/status` every 3 s, publishes `source.state`, and keeps the quiet
  task `feed` running. After a feed batch that changed the archive it publishes
  `kb.changed`.
- `kb/feed.py`: parses and applies records through one Sink; edits and deletes whose
  message has not arrived wait in `feed_pending`. Records of an ignored chat are skipped.
- `kb/ignore.py`: "Delete and ignore". One list for every source, `ignored_chats`, which
  every Sink keeps to.
- `mcp.py`: a block with `command` next to `url` is started only when the url does not
  answer; it outlives the app. A connector only reads: a tool of its that is not
  read-only is never offered to the agent. Tool results' records are archived and rendered
  as Messages archive lines, so they are citable.
- `api/connectors.py`: the source pages' routes.

Write scope, enforced in the connectors and proven by their tests: discord-link's REST
client refuses any method but GET and nothing else writes to Discord; wa-link sends no
message, only two requests to the account's own phone (older messages of one chat, a
media retry receipt).

### Semantic

`kb/semantic.py`, built on its own every `smart_search_refresh_minutes` (a general setting
shown on Settings, Search index; 0 = off) and after a settings change. Each chat is cut into sessions at pauses, sessions into overlapping passages, and
every passage without a vector is embedded (`kb/sessions.py`, `kb/passages.py`,
`kb/embed.py`). `kv.passages_vec` records the width and the model's fingerprint; another
fingerprint resets the index. Vectors are the first `dims` dimensions as int8 plus sign
bits: a Hamming scan picks candidates, int8 cosine rescores them. Until a knowledge base's
first build has finished (`kv.passages_ready`), retrieval there is words only. The agent
searches through `kb/retrieve` (word search and semantic topic search, RRF, the reranker,
match windows).

### kind = documents

`kb/documents.py` (+ `documents_extract.py`, `documents_describe.py`). Your drawer, shared
with the agent, tied to no chat. The files under `path` are the content, kept byte for byte,
in folders; `var/kb/documents.db` holds items (stable ids, the mode, your title and
description, the AI description, `author`, `trashed_at`), `trashed_folders`, segments (the
extracted text, quotable), a contentless normalised FTS (`search_fts`), the
passages/vectors of kb/passages.py and `[[...]]` links. A folder move is one rename on
disk plus a path prefix update, so ids stay. Every change emits `documents.changed
{path}`. The mode decides the indexes: a chunked file's segments are in `search_fts` and
cut into passages; a full-file document has only its names and descriptions row.
`sync()` compares mtime and size, hashes only what changed and recognises moves by hash;
it runs at startup and from a `watchfiles` watcher. Reading runs in the task `documents`,
AI descriptions in `describe` (the LLM queue at background priority; a long file is
map-reduced). Paths are confined to the folder; hidden files are ignored; writes go
through a temp file and a rename.

### Tasks

`kb/tasks.py`. A task is a thread and an in-memory row: progress, result, error, cancel
flag, retry. Events go to the global feed as `kb.task`, `kb.job` and `kb.changed` through
`loop.call_soon_threadsafe`, because the bus is not thread safe. Tasks do not survive a
restart; every task is idempotent. A run ends done, cancelled, failed or unreachable
(`Unreachable` is a normal end). A quiet task is never listed; `due_at()` is the one retry
rule for scheduled work.

### Skills

`chat_archive`: `search`, `count`, `look_up`, `read_session`, `read_period`, `scroll`,
`get_messages`, `mark_same_person`. `user_documents`: `list`,
`search`, `read` and `write` (a text file into the drawer; it replaces only files it
wrote).

## Attachments

`attachments.py`. `POST /api/attachments` (multipart) streams to `var/blobs/tmp`, hashing
as it goes, refuses past `max_attachment_bytes`, then moves the file to
`blobs/ab/cd/<sha256>` and writes a `blobs` row (once per hash) and an `attachments` row
with `message_id NULL`. Kind is `image`, `text` or `binary`. `POST
/conversations/{id}/messages` takes `attachments: [ids]`; each is linked in the same
transaction as the message. `GET /api/attachments/{id}/file` serves the file under its
own name and type: raster images and audio inline (`?download=1` saves them), everything
else as a download, always with `nosniff` and a sandboxing CSP. A file that joins a
message already there (one it sends from its box, `shell__send`) sends
`message.attachments` with the message's whole list; its reply shows the files that no
batch made.

For the model, `attachments.render` turns a user message into content parts: text
attachments up to `inline_text_max_bytes` are inlined, everything else is a one-line note,
and an image stays an `attachment_image` part until the model that answers is known. The
queued backend (`backends/llm.py`) hands each request to `attachments.Images.wire` for the
model it goes to:
- a model with `vision` gets the image as an `image_url` data URL (a model with a
  `vision_twin` in Settings sends such a request under the twin's name,
  `QueuedBackend._model_for`);
- any other model gets `[Image name, described by <photo model>: <description>]`. The
  Photo descriptions role writes the description the first time (`kb/media.py`
  `Engine.describe`) and it is kept with the attachment (`description`, `described_by`),
  and the event `attachment.described` gives the message its note;
- when no Photo descriptions model answers, the model gets a line that the image is there
  and it cannot see it.

The composer asks `GET /api/chat-model?conversation=&model=`: `{model, tools, sees,
describer, blocked}`; `blocked` (the model can't see images and no describer answers)
turns Send off, and `POST .../messages` refuses the same case (`cannot_see_images`). A
switch to a model without `vision` in a conversation that holds images leaves one
`images.notice` event. A server that refuses an image fails the turn with
`unsupported_input`.

A file a tool makes (`attachments.save_generated`) is an attachment of the message that
holds the call; a file a batch makes is also the batch's (`batch_id`). A picture the tool
keeps for the user alone is an attachment of the call (`tool_call_id`, no `message_id`).
An image a tool gives it with its result (`ToolResult.images`) is linked to the call's
`role=tool` row by `finish_tool_call`, and `turns/context.py` renders it through
`attachments.render`; an aged result goes as its stub, without the image.

Startup GC (`attachments.gc`, its rule in the module docstring): a blob stays while an
attachment or its portrait points at it; a draft stays until it is sent, removed or 30
days old (`KEEP`); a blob nothing points at goes once it is 30 days old. Deleting a
message cascades its attachment rows, so a later GC frees the blob.

Web: paperclip, drop anywhere on the transcript, or paste. Each file uploads at once
and shows as a chip; a failed chip carries the error and can be removed; send waits
only for uploads in flight. The ready chips are kept with the conversation's draft
text, so a reload or a switch keeps them. The bubble shows images inline and other files as chips.

## Images and music

Everything ComfyUI makes is made in a batch (`comfyui/`): a list of items, rendered in
order under one GPU lease. The agent starts one with `image_generator__generate` or
`music_generator__generate`, the user with `POST /api/batches` (`kind` images or
music). One code path for both (`batches.Batches`).

- **Presets** (`comfyui/presets.py`): `config/image_presets/` and `config/music_presets/`,
  a pair of files per preset: `<id>.toml` describes it for people and for it (label,
  family, good_at, prompting, task, parameters, size, the catalog of models and LoRAs) and
  `<id>.json` is the ComfyUI workflow in API format, whose values are the defaults.
  `load(kind)` checks each preset; every problem is a sentence in the configuration
  banner and a preset with one is left out. The user's presets are pairs in
  `var/settings/image_presets/` and `music_presets/` (Settings, Images & Music): an edit
  of a shipped one under its id (Reset to defaults deletes it) or an upload of their own.
  An upload is a workflow in API format whose filled inputs hold placeholders
  (`"{{prompt}}"`, `"{{width:1024}}"` with a default); the placeholders become the
  parameters, and one a preset needs that is missing refuses the upload with a sentence
  each. A ComfyUI provider offers every preset as a model, `image_generation` or
  `music_generation` by its folder.
- **A batch** (`comfyui/batches.py`, table `media_batches`): `{id, kind, conversation_id,
  tool_call_id, state, created_at, finished_at, items}`, one shape for the routes, the
  event and the display of its call. `state` is running, done or stopped. An item is the
  request and its slot (`index`, `state`, `seed`, `preset`, `provider`, the request,
  `attachment?`, `seconds?`, `error?`, `note?`). `count` repeats each item with the next
  seed; a batch holds at most 16 slots.
- **Create** checks every item first: the preset, the size (`Preset.fit`), the mode, and
  an edit's source (an image it made, or one the user attached to this conversation). An
  item that fails a check is failed in its slot with the sentence why; the rest go on.
- **Run**: before the first item of a provider, its GPU lease (`backends/llm.py
  gpu_lease`), held to the end of the batch. Each item is queued, followed and fetched
  (`server.run`); a failed item fails its slot only. What an item made is an attachment of
  the batch (`attachments.batch_id`), kept as a file in `images/<date>/` or
  `music/<date>/`. At the end every server used frees its models (`POST /free`) and the
  leases are released. A restart stops the batches it cut (`batches.sweep`) and frees the
  shared servers (`server.free_shared`).
- **State**: every change is one write: the row, the display of the call that started it,
  and the event `images.batch` or `music.batch` with the whole batch, on its
  conversation's stream and the global feed.
- **The agent's call** is a blocking tool: it creates the batch, starts it in the app's task group
  (`Batches.begin`), and answers at once with the batch id and its slots. The batch is no
  job: nothing wakes it. `wait=true` holds its turn for the end, as long as its time
  allows. `read` shows a batch, `stop` stops one. `image_generator__list_presets` lists
  the presets or a preset's models and LoRAs (`image_catalog.py`), `list_images` the images
  of the conversation.

**The agent's portrait** (`agent.set_portrait`): one image, a blob its row names, png, jpg or
webp, the kind read from its bytes. The user sets it (`PUT /api/agent/portrait`); it sets it itself with the `appearance` skill's
`appearance__set_portrait(image)`: an image by its short id under the same rule as an
edit's source (`attachments.agent_image`: one it made, or one the user attached to this
conversation), or a Documents image by its id, path or title. Either way
`agent.changed` goes out; `GET /api/agent/portrait` serves it.

## Triggers

`triggers/`, started after the dispatcher. Triggers are background tasks on an event,
never a chat. A skill declares its own in code, `triggers = (...)` on the skill class:

- `Watch(name, path, events, prompt, debounce)` runs `watchfiles.awatch` on the path; one
  batch of changes is one fire, `{path}` becomes the matching paths one per line and
  `{event}` the kinds seen.
- `Event(name, source, prompt, every)` asks the skill's coroutine `source(store)` every
  `every` seconds; each dict it gives is one fire, its keys filled into the prompt. The
  email skill's `mail.new` is one.

The state is the settings section `triggers` (`var/settings/triggers.toml`): `on` per
trigger, every trigger off until you turn it on. `apply()` runs at start and after every
settings save and config reload. `GET /api/triggers` gives each with `fired_at`.

A fire finds or creates the trigger's conversation by `trigger_key` (`trigger:<name>`,
kind `background`, never in the chat list) and sends it an intercom `trigger` message from
`system:<skill>`. The text is the prompt and the rule: nothing that needs you ends the
turn with one line for its log; something actionable becomes a chat for you with
`intercom__send(to="user", subject, text)`. From a background conversation (a trigger's
task, a Routine's run) a message to `user` always goes this way (`intercom/skill.py
_chat`, the one path `intercom__start_chat(title, text)` takes too; that tool is offered
in kinds background and mission): it looks up what you already have from the same source
in the last 24 h (a bug report still open, a thread to you) and lists it in its result;
it creates a chat whose first message is its text, keeps in kv (`chat_opening:<id>`)
where it came from (with the id `past_conversations__read` takes: `run:<id>` for a run,
else the conversation's), and sends the text to `user` from that chat, so your answer
lands there, never in the finished run. The subject (title) is 2 to 4 words.

## Schedules

`schedules/`. A schedule is a job as data: `var/settings/schedules/<name>.toml`, copied
once from `config/schedules/` at the Scheduler's first start with the reviewer prompts
beside them (`*.md`), read only from there. Fields: `label`, `summary` (one sentence for
the onboarding's list), `cron` (the app's time
zone), `prompt`, `after` (Runs after: the schedules whose runs of the same night it follows),
`reviewer` (the Reviewer: a prompt file it talks with first) and `turns` (Turns: the
reviewer's lines at most), `note` (a notebook path the
answer is written to), `role` (chat, tasks or small, default chat), `on`, `idle_only`,
`wall_minutes`, `daytime`, `on_fail` (When a run fails: `retry`, the default, or `skip`;
Daydream, Followed sites and Watch videos ship `skip`).

**Variables.** The prompt, the reviewer prompt and the note are filled from the run's
night, never from the clock (`schedules.fill`): `{{date}}` (for a `daytime` schedule the
calendar date of its slot), `{{day_before}}`,
`{{weekday}}`, `{{result:<schedule>}}` (that schedule's answer of the same night), the
agent's `{{name}}` and `{{pronouns}}`, and `{{username}}`. When the run starts, `schedules/variables.py` fills
`{{mood}}`, `{{on_mind}}`, `{{tool_failures}}` and `{{voice_counts}}`.

**Runs** (table `runs`): one schedule at one slot, its planned time (UTC); `night` is the
night of the slot (`night_of`: before 12:00 in the app's zone belongs to that date's
night, from 12:00 on to the next date's); `state` waiting, running, done, failed, skipped
or stopped; `reason` why it waits; `conversation_id` its one conversation; `result`,
`error`; `started_at`, `finished_at`; `again_of` the run a retry retries; unique
(schedule, slot). Every change is `run.state {run}` on the global feed; its view adds
`retried` (the retry of this run, shown as Retried), `reason_why` (the reason without the "Waiting"
its state word says) and reads `waiting`, its reason "Waiting for the model" (`runs.HELD`),
while the running run's work waits for a model.

**When a run exists.** At the next slot, on every `models.state` change, at the end of
every turn and after every change to the schedules, the scheduler (`schedules/runs.py`,
one per app) makes the run of every slot of a schedule that is on and came due since its
last look (kv `schedules.checked`). Slots missed while the app was off are made too: those
of the last 7 days wait like any other, older ones are written `skipped`. A Skip routine
waits for its newest slot only: its new waiting run ends its older waiting ones skipped
("The next run was due", `NEXT_DUE`). A run the user started (`started_by` user: Run now,
Retry on its page) neither ends other runs so nor is ended by a newer one, and the idle
rule (`idle_only`) never skips it.

**When a run starts.** A run waits until all of these hold; its `reason` says the first
that does not: its slot has come (a retry waits for its time, "Retry of the 02:00
run"); every run it follows in the same night is finished; the first model of
its role can answer now (`Router.can_answer`, "Waiting for <model>. <why>", the why in
the server's words; a server known to be failing is asked again at most every 10 s); no
turn you wait on is live (a chat's, or one you started in a mission; with Routines wait
for missions on, any turn of a mission: `runs.waited_on`); no other run is running. The queue is ordered by night, then by how deep the
schedule follows, then by slot.

**A run** is one background conversation (`trigger_key` `run:<run id>`) on the
schedule's role, its turns bound behind every interactive request and on the role's own
model only (`runs.binding`, the dispatcher reads it, for the run's conversation and every
sub-agent under it at any depth), with the filled prompt as its first
message from `system:schedules` and a rule: you are not here, its final answer is the result, `intercom__send(to="user")` for what
needs you, which starts a chat for you (Triggers). Its lines are .md files beside `schedules/runs.py` (background.md, final.md,
note.md, and reviewer_first.md, asked.md, line.md, last_line.md, talk_over.md for a
reviewer). With a `reviewer`, it answers the prompt first, then the reviewer (on the run's
role, outside the turn loop, with read-only tools) and the two alternate up to `turns` lines;
its lines land as intercom messages from `system:<reviewer>` (the file's name without `.md`). Then it is asked for its
final answer. The run is over when its conversation and its sub-agents are quiet and
every sub-agent has reported. The result is written to `note` with `notebook__write`: a
note that was there gets the answer appended under "## <date> <label>"; a note it wrote
itself is the result and the app writes nothing over it. A turn that ends with a tool
call that is not the note's write has no answer: the run is done with the lead-in beside
that call as its result, the note is not written, and the run's `error` says so. A note
it wrote and then went on past (ok tool calls after its last write of it, the note older
than the last of them) is the result still, and the run's `error` says how many calls
came after it.

**When a run fails.** A run is never re-asked or rewritten. When its turn fails and the
Router says the role's model cannot answer (unreachable, or none set), the run is `waiting`
for it ("Waiting for <model>"), its conversation kept; when the Router sees the model
answer, the failed turn is continued in that conversation (`turns.continue_turn`, the
stored context). Any other failure (a model error, a tool error that ends its turn, its
turn stopped, its ceiling) ends it in one place, `runs._fail`, as its schedule's
`on_fail` says. Skip: `skipped` with the error as its reason and no notice; its look back
reads "run Daydream, skipped". Retry: `failed` with the error; a run that failed
on an error (not its ceiling, not a stop) gets a fresh run of its prompt for the same
night, up to three times, 15 minutes, 1 hour and then 4 hours after the try before
(`RUN_AGAIN`), each `again_of` the one before, its dates the first try's, only when that
is before the schedule's next slot, and the runs after it wait for it;
the notice goes out only when no retry follows. `POST /api/runs/{id}/retry` retries a
failed, skipped or stopped run now (400 while it has not ended, once it was retried
or its schedule is gone). The ceiling is the Routine time limit (`routine_max_minutes`,
Settings, Advanced: 180 minutes by default, 0 none), or the schedule's `wall_minutes`
when that is shorter (`runs.py _start`). It
counts work, not waits: while any call of the run, its sub-agents
or its reviewer waits for a slot, a busy server or a GPU lease, the run's clock
(`runs.RunClock`, an `llm.HeldClock` made at the run's start and bound with its turns)
stands still, overlapping waits count once, and the run reads `waiting` ("Waiting for
the model"). A missing model
is no such wait: the run's turns, its sub-agents and its reviewer are bound with
`fallback` False at background priority, so a call there fails at once: a turn that
fails so makes the run wait for its model as above, and a tool's model call (vision,
models__ask, Writing) gives its error to the turn as the tool's result. A request waiting
for your answer does not stop the clock: it waits as long as the run's limit lets it. A run that waited for
its model starts with a new clock. A run that was running when the app stopped is still running at the next start:
the scheduler continues its cut turn and takes its answer once the
conversation is quiet (a reviewer's talk that was cut ends there). Stop (`POST
/api/runs/{id}/stop`) ends a waiting or running run as stopped; deleting a schedule skips
its runs.

**Routes.** `GET /api/schedules`; `PUT /api/schedules/{name}` (the whole schedule; the
prompts go into their history as `texts` `routine:<name>:prompt`); `DELETE
/api/schedules/{name}` (refused while another follows it); `POST /api/schedules/{name}/run`;
`POST /api/schedules/preview {night, text}`; `GET /api/runs?night=&schedule=&until=` (each
night `{night, failed}`, failed when a schedule's newest try that night failed); `GET
/api/runs/{id}`; `POST /api/runs/{id}/stop`; `POST /api/runs/{id}/retry`. `GET
/api/schedules` also gives `choices` and `help` for When a run fails, Only when the app
is idle and Time limit. `GET
/api/background-conversations` gives a run's conversation its `run` {id, schedule, label, night}. The web
place is Routines (`views/Routines.tsx`).

## Intercom

`intercom/`. Durable, addressed, threaded text messages between you, its conversations
(agents) and the app, like email. Every path by which something lands in a conversation
later is one intercom message: your message, a finished or stopped job, a reminder, a
sub-agent's Instructions and report, a trigger, a restart notice, a mission's Instructions. Rows are
never deleted.

Tables: `intercom` (one row per message: `from_addr`, `to_addr`, `kind`, `subject`, `text`,
`data` JSON for code, `wake`, `due_at`, `until_at`, `reply_to`, `thread`, `cancelled_at`),
`intercom_deliveries` (one row per recipient: `to_addr`, `conversation_id`, `state` pending,
delivered, read or cancelled, the `message_id` and `turn_id` it landed as) and
`intercom_tokens`. Kinds: `message`, `brief`, `job`, `report`, `trigger`, `notice`.

**Addresses** (`addresses.py`): stored `user`, `agent:<conversation id>`, `team:<conversation
id>` (that conversation and its sub-agents; the word `team` is the sender's own), `all` (every conversation with a live turn or
a running job, and every active mission), `system:guard` (Bastet) and `system:<name>` (a
sender only: `system:app`, `system:<skill>`, a named token). The agent's words `me`, `parent`,
`team`, `all`, `user`, `bastet`, a name, an id, `run:<id>` or `routine:<name>` are resolved
inside the send, the sender left out.

**Threads.** A message starts a thread (`thread` = its id) unless it has `reply_to`; then it
joins that message's thread. A reply without `to` goes to the sender of the message it
answers. Lists of threads (`store.threads`, `store.grouped`; `intercom__read` and `GET
/intercom/threads`) group them by counterpart; your plain chat messages (`store.PLAIN`),
the app's notices into a mission (`store.APP_TO_MISSION`: its keep-going line, its time
limit, a restart; they are the mission's transcript) and what lands in a chat as the
agent's own message (`store.SAY`) are never threads.

**Every message to you has a home** (`intercom/skill.py send`, `_to_user`), by where
`intercom__send(to="user")` is called from, and your answer goes back there:
- a chat: you read it there, so nothing is sent with no time (the result says its words
  in the chat reach you); with `in` or `at` the text goes to the chat itself with
  `data.say`, and at its time it lands there as the agent's message (role assistant,
  never mid-turn: `take` leaves it for the turn's end) with the reply notice;
- a background conversation (a trigger's task, a run): a chat of its own (Triggers);
- a mission: the message to `user` as written. It waits on the mission: the mission's page
  shows what waits (`GET /intercom?from=agent:<id>&waiting=1`) in its Waiting for you,
  whose reply box answers the newest with `reply_to` (the thread is read, a plan's
  checkpoint and Needs you clear as with any reply), and Waiting for you lists the
  mission once, not its thread (`store.MISSION_FROM`); the thread is in the All tab.
The service itself sends what it is given (an answer in a thread, `answer.py`; a bug
report).

**The delivery rule** (`deliver.py`), one rule for every kind. A delivery to a conversation
waits until it is due. Then: when the conversation is idle, `deliver_idle` appends every due
delivery under the active leaf, one user-role message each (`tree.append`, author `user` for
your own message as typed, else `intercom`), and when one of them has `wake` and the
conversation is not a stopped mission, queues one turn rooted at the last. It runs in the
send itself, at every turn end and when a due time passes. While a turn is live nothing
outside the runner touches the tree: the runner's `take` appends the due deliveries under
its own tail before each model request. The delivery row records what happened in the same
write that appends the message, so nothing lands twice. Deliveries to `user` stay `pending`
until you reply in the thread or dismiss them (`POST /intercom/read`).

**The service** (`service.py`): `Intercom(store, notify)`, in `Services.intercom`.
`send(from, to, text, kind=, subject=, data=, wake=, due_at=, until_at=, reply_to=,
attachments=)` writes the message and delivers it to every idle recipient in one write and
returns `{intercom_id, message_id, turn_id, seq}`. `cancel(id)` cancels a scheduled message
that has not landed. `run(tg)` starts the timer: at start it delivers what came due while
the app was down. Every send, cancel and read publishes `intercom.state` on the global
topic.

**The header** (`render.py`) is rendered from the row at request time, never stored:
`[From scout (#412, re #409, "logs")]`, `[From you (#7, set Thu 1 Oct 23:10)]`, `[Brief
changed by you (#9)]`, `[Job 3f2a1b9c (shell__run) ended]`, `[Report from scout (#415)]`.
The snapshot carries the same fields as `message.intercom` for the web.

**Announcements.** A message to `all` with `until_at` is delivered to everyone working when
it is sent and, until `until_at` passes, is listed in the instructions of every turn that
starts.

**Named tokens** (`tokens.py`): you name a token in Settings, Security; it is shown once and
stored as its sha256. A request with `Authorization: Bearer <token>` passes the login gate
on `/api/intercom` and `/api/intercom/*` only, and sends as `system:<name>`; every other
route and every `/ws` socket answers it the gate's 404.

**Missions** (`missions/watch.py`, on `dispatcher.on_end`): when a turn of an active mission
ends done or capped and nothing will wake it, one `notice` from `system:app` with
`data.keep_going` starts its next turn, while a step of its plan is open or it has no plan
yet. When the turn only such a line started called no tool, or ended another way, or every
step is done, the mission moves to `needs_you` (Waiting for you). `missions__wait(why, in | at)` books its wake.
Its result and its questions live in the mission (`missions/steps.md`): Done with its
artifact and a note, Needs you with the question as its note; both are a `mission`
notice to you (`missions/store.set_status_sync`) and a `report` to the conversation it
reports to (`missions/skill.record`).
Check-ins (`watch.run`): an active mission with `check_in` minutes sends a `report` to its
report conversation every `check_in` minutes, else a `mission` notice to you (toast and
push, Notifications: Missions) that opens the mission's page, no Intercom message; the
last one's time is `missions.checked_in_at`. The app writes it, so it costs the mission
no turn.

## Events

One list of everything it did and everything that ran (`server/celine/events.py`), built
from the rows that exist, nothing stored twice. Each source is one SELECT with the same
first columns and a view that gives the row its words: `turn`, `tool`, `notebook`, `job`,
`message`, `run`, `guard`, `freeze`, `pass`, `work`, `agent` (its own edits of its persona,
Example lines and banned words) and `bugreport`. Each tab of the place lists its own
sources (`events.TABS`): Guard (`guard`, `freeze`, `pass`), the agent's own tab (`agent`, labelled with its name)
and Background tasks; a turn, a call or a notebook write lands in the agent's own tab or
Background tasks by its conversation.

`GET /api/events` merges them newest first
by `(at, id)`; a page's `next` is that pair. `links.open` is the app path where it
happened (`intercom/store.py source_of` for a conversation, a note, a run, a thread).

`events.added {kind, conversation}` on the global feed says one landed: `Store.event` sends
it in the same write for the conversation events `core/bus.py lands` names; the other
sources call it themselves.

## Waiting for you

One list of everything that waits on the user (`server/celine/waits.py`, `GET /api/waits`
`{items, count}`): every request it makes, as its card (`approvals.py`), every mission in
`needs_you` or with a message of its own that waits for them, once (that message, else its
note), every chat source whose login was lost, every other Intercom thread with a message
that waits for them. A freeze and a pause have their own rail items; runs belong
to Routines. `waits.run` turns each `approvals.changed`, `mission.state`, `source.state`,
`intercom.state`, `conversation.deleted` and `conversation.restored` into one
`waits.changed`, and the client asks the list again; it counts nothing itself.

## Bug reports

`server/celine/bugreports/`, off with `[bugreport] enabled = false` in `config/skills.toml`.
It files a problem with the app with `bugreport__file(title, severity, what, where,
wish)` (`skill.py`, guide `guides/bugreport.md`). The rows are `bugreports` (`store.py`),
open until you close one. Each report is an Intercom thread: filing it sends a message to
you from the conversation that filed it (subject `Bug #<id>: <title>`), its id in
`bugreports.thread`, and your reply wakes it in that conversation. `GET
/api/bugreports?thread=` and `?state=open|closed` list them; `POST
/api/bugreports/{id}/close` `{why?}` closes one, and `why` goes into its thread as the
app's line `Report #<id> closed: <why>`, delivered to the conversation that filed it, so
the agent reads why. `bugreport__list(state)` is its own view: each report's state and
the last word on it from you or the app, which the guide tells it to read before filing.

The web place is `views/Events.tsx` (`/events`); a row opens `links.open`.

## MCP

`mcp.py`. One supervisor task per enabled `[servers.*]` block, started before the
dispatcher. Each server is a skill named after its key, tools `server__tool` with the
server's own input schema, gated like the built-ins. `stdio` runs `command args` with
the SDK's default environment plus `[servers.*.env]`; `http` uses streamable HTTP with
`[servers.*.headers]`. Header values are in `Config.secrets()` and redacted.

Life cycle: connect, `list_tools` within `startup_timeout_seconds`, register, cache the
list in `mcp_tools`, then `list_tools` every 15 s. A failed poll or transport error tears
the session down and reconnects with backoff (1 s doubling to 30 s). Past `max_restarts`
failures in a row a server with a `command` is `dead`; `POST /api/skills/{name}/restart`
resets the count. A server at a `url` alone is never `dead`: it stays `down` with its
error and reconnects at the backoff until its host answers again.
`/api/skills` carries `status` and `error` per skill.

## HTTP

With the login on, the session gate (`auth/gate.py`) wraps the whole app: without a
session every route and every WebSocket answer the 404 of an unknown route, except the
login page at `/` and the calls it makes (`auth/routes.py` `open_router`; docs/DEPLOY.md, "Login").

Every route and the WebSocket check `Origin` and `Host` (`api/origin.py`): loopback,
and with the login on also the public origin's host and `[server] allowed_hosts`. A
state-changing request without an `Origin` header is refused; with `--dev` the Vite
origins are allowed.

REST, all under `/api`, JSON errors as `{"error": {"code", "message"}}`. The login
is in docs/DEPLOY.md.

| Method, path | Does |
|---|---|
| `GET /status` | `owner` (it, as every page draws it: `{name, pronouns, color, portrait, named}`), `user`, `problems` (config problems), `hosts` (each provider up, down or null, as the app last learned it), `hold` ("paused" or null), `features`, `setup` and `catalog` (the features), `places_off` (the places that leave the rail: no feature of theirs on and set up), `build` (`{commit, built, source}`), `vault_locked` |
| `GET /agent` | its package: `name, pronouns, color, portrait, named, persona` (as shown, names filled), `talk` (null: the shipped one), `talk_shipped`, `voice {banned, phrases, patterns, bias, on}`, `voice_text`, `voice_help`, `version` |
| `PUT /agent` | `If-Match: <version>`; any of `name, pronouns, color, persona, talk` (null: back to the shipped one), `voice`, checked whole before anything is written; global event `agent.changed` |
| `PUT /agent/portrait`, `DELETE /agent/portrait`, `GET /agent/portrait` | its portrait: multipart `image` (png, jpg or webp) or JSON `{attachment}` (one by its id), remove, serve |
| `GET /conversations?limit=&cursor=&archived=&trashed=&place=` | a page: running first (a live turn in it or a sub-agent of it; a queued turn while it is frozen does not count, and its row has no `state`), then pinned, then by `updated_at` |
| `POST /conversations/{id}/branch` | Branch from here: `{message_id}` -> `{id, title}`, a new chat of the same settings with every message of the path up to and including that one |
| `GET /conversations/{id}/export` | Export: the path as a Markdown download |
| `GET /agent/mood` | its mood next to its name: `{mood: {text, set_at} | null}`; it sets it with `mind__set_status(text)` |
| `PATCH /conversations/{id}` | `{title?, pinned?, notes_off?}`; any other key is a 400 (the kind is fixed at creation) |
| `POST /conversations/{id}/archive`, `POST /conversations/{id}/unarchive` | the conversation and its sub-agents leave the list (or come back), nothing is removed and its search still finds it; `{archived_at}` |
| `POST /bulk-actions` | one action on many |
| `DELETE /conversations/{id}`, `POST /conversations/{id}/restore` | the user's delete: the conversation (any kind) and its sub-agents are trashed (`trashed_at`), nothing is removed; 409 `busy` like archive |
| `POST /conversations` | `{title?, model?, thinking?, tools?}`, all checked before it exists; `{id, settings}` |
| `GET /conversations/{id}` | snapshot: active path, tool calls, turn, seq, `queue` (the live turns in the order they run, `[{turn_id}]`), `conversation.archived_at`, `guard` (Bastet's banners, "Bastet") |
| `POST /conversations/{id}/messages` | `{text, attachments?}`: an intercom message from `user` (`conversations/api.py`); `{intercom_id, message_id, turn_id, seq}`, the last three null while a turn is live |
| `POST /conversations/{id}/stop` | |
| `POST /conversations/{id}/regenerate` | answer the last user message again |
| `POST /conversations/{id}/turns/{tid}/continue` | `{turn_id, seq}`; 404 `not_found`, 409 `busy`, 400 `not_continuable` with `detail.reason` |
| `POST /conversations/{id}/retry` | `{message_id}`, new sibling, generate |
| `POST /conversations/{id}/messages/{mid}/versions` | `{text}`: a new version of the message (The message tree), the active leaf; `{message_id, turn_id, seq}`, `turn_id` null for its reply (no turn runs) |
| `POST /conversations/{id}/messages/{mid}/continue` | its finished reply at the active leaf carried on as a new version (The message tree), which keeps the reply's time |
| `POST /conversations/{id}/impersonate` | `{start?}`: the user's next message as the chat role writes it, beginning with what they typed (`start`), one request with thinking off |
| `PUT\|DELETE /conversations/{id}/messages/{mid}/voice-sample` | More like this: its reply as a voice sample in its note `samples.md` |
| `PUT /conversations/{id}/active-version` | `{message_id}`, moves the pointer |
| `GET /conversations/{id}/messages/{mid}/siblings` | |
| `DELETE /conversations/{id}/messages/{mid}` | the user's delete, in every conversation kind: the message and every message after it in its branch, for good; `{message_ids, active_leaf_id}` |
| `PUT /conversations/{id}/tool-settings/{target}` | `{enabled, client_seq}` |
| `GET /skills?conversation=` | catalog with per-conversation state: only what its turns can get (`loads.offered`) |
| `POST /skills/{name}/restart` | restart a dead MCP server |
| `GET /jobs?state=&conversation=&limit=&cursor=` | a page, running first then newest: `{jobs, next}`; each job with the call it ran (`tool_call_id`, `message_id`, `started_at`, `display`, `progress`) |
| `GET /jobs/{id}` | one job, the same shape |
| `GET /triggers`, `PUT /triggers/{name}` | the skills' triggers and the user's state of one  |
| `GET /schedules`, `PUT`/`DELETE /schedules/{name}`, `POST /schedules/{name}/run`, `POST /schedules/preview` | the schedules (Schedules) |
| `GET /runs?night=&schedule=&until=`, `GET /runs/{id}`, `POST /runs/{id}/retry`, `POST /runs/{id}/stop` | their runs (Schedules) |
| `GET /background-conversations` | the background conversations (fired triggers and runs), newest activity first: `{background: [{id, title, kind, trigger, working, last_line, updated_at}]}` |
| `POST /jobs/{id}/stop` | `{cancelled}` |
| `GET /generation-presets?kind=images\|music` | every preset of the kind as the UI shows it, with its editor's `fields` and `values` and `origin` (shipped, edited, own) |
| `POST /generation-presets`, `PUT`/`DELETE /generation-presets/{id}`, `PUT`/`GET /generation-presets/{id}/workflow`, `POST /generation-presets/{id}/reset` | the user's presets (api/presets.py): an upload `{kind, label, workflow}`, the editor's `{values}`, another workflow `{workflow}`, the workflow with its placeholders as a file, an edited shipped one back as shipped; 400 with every reason, nothing saved |
| `GET /batches?kind=&conversation=&limit=` | batches of the kind, newest first: `{batches}` in the one shape (Images and music) |
| `GET /batches/{id}` | one batch; 404 for an unknown id |
| `POST /batches` | `{kind: "images", items: [{prompt, source?, preset?, negative?, width?, height?, seed?, model?, loras?, mode?}], count?}`: the user's batch, no conversation; `source` is an image attachment's id |
| | `{kind: "music", items: [{style, lyrics?, title?, instrumental?, duration?}]}`: the same for music |
| `POST /batches/{id}/stop` | `{stopped}`: false when it was not running |
| `GET /messages?q=&limit=&place=&archived=&trashed=` | words in the conversations with the agent: `{hits, titles}`; each hit with its `message_id`, `snippet` and `marks` ([start, end) of each matched word in it), best first |
| `POST /attachments`, `DELETE /attachments/{id}`, `GET /attachments/{id}/file` | files for a message |
| `GET /notebook/tree`, `GET\|PUT\|PATCH\|DELETE /notebook/entries/{path}` (PATCH `{path}` moves, `{protected}` protects), `POST /notebook/folders`, `GET /notebook/trash`, `POST /notebook/trash/{tid}/restore`, `DELETE /notebook/trash/{tid}` | the notebook browser (its one notebook); a PUT keeps the note's text before in `.versions` |
| `GET /notebook/versions?path=&q=&before=`, `GET /notebook/versions/{vid}?path=` | a note's History (the editor's `HistorySource`): `{versions, current, total, next}`, `versions` the texts before, newest first, 50 a page (`before` the id to page from) |
| `GET /llm-queue` | the model queue now (fetched again after a reconnect) |
| `GET /loaded-models` | what llama-swap has loaded, probed: `{models: [{ref, loaded, load_seconds}]}`; `load_seconds` the last load the server timed (null before one, kept in memory) |
| `GET /slash-commands?conversation=` | the slash commands: `{commands: [{name, aliases, usage, description, where, when, args: [{name, description, required, kind, choices, suggestions}]}]}` |
| `POST /conversations/{id}/slash-commands/{name}/run` | `{text}` (everything after the name) → `{command, ok, state, message, …}`; 404 `unknown_command`, 409 `busy`, 400 `bad_args` / `unknown_model` / `not_added` |
| `GET /terminals?mission=&trashed=` | its terminals and the user's (`terminals/api.py`), open first, trashed ones left out (with `trashed` only those): `{terminals}` |
| `POST /terminals` | a terminal of the user's own: the first free name of theirs (`name`, `name-2`) |
| `GET /terminals/{id}` | one row, the shape of the list's |
| `GET /terminals/{id}/log` | the terminal's whole stored history, raw bytes (`application/octet-stream`; the `.raw.gz` of an ended one unzipped); 404 for an unknown terminal |
| `PATCH /terminals/{id}` | `{may_type}`: whether the agent may type into this terminal of the user's; 400 on one of its own |
| `POST /terminals/{id}/control` | `{action}` stop, pause, resume or close → the daemon's `/control`; logged as a command of the user's (the button's label, the short word, on its terminals also the sentence it reads next) |
| `DELETE /terminals?state=ended`, `DELETE /terminals/{id}`, `POST /terminals/{id}/restore` | every ended terminal, or one, into the trash (`trashed_at`), or one back; nothing is deleted: their page and the commands log still reach them; `{trashed}`, `{ok}`. `DELETE /terminals/{id}?for_good=1` removes a trashed one |
| `GET /terminal-commands?terminal=&from=&result=&q=&mission=&limit=&before=` | the commands log, newest first: its shell calls (and what a server shows there, `place = "terminals"`: the Mac's shell) and the user's typed lines and controls as one list |
| `POST /terminals/{id}/messages` | `{text, conversation_id?, command_id?}`: the user's text into the terminal's last conversation, the given one, or a new one (`"new"`) |
| `GET /artifacts` | its pages and documents, newest change first (`artifacts/api.py`): `{artifacts: [{id, kind, conversation_id, created_at, trashed_at, n, title, note, updated_at}]}` |
| `GET /artifacts/{id}` | one of its pages |
| `GET /artifacts/{id}/versions/{n}` | the page itself, with its own headers (`artifacts/page.py`): CSP `sandbox allow-scripts allow-forms` and the library hosts |
| `GET /artifacts/{id}/versions/{n}/source` | what it wrote, as `text/plain` under the page's headers: the Download, and the text the app renders a document from |
| `POST /artifacts/{id}/versions/{n}/restore` | that version copied to the top as a new one with the note `restored from v<n>`; the artifact as `GET` returns it; publishes `artifact.updated` |
| `DELETE /artifacts/{id}`, `POST /artifacts/{id}/restore` | into the trash, or back out of it (`trashed_at`); a trashed one still opens by its id; the artifact; publishes `artifact.updated` |
| `GET /shares?conversation=` | its shares that run, newest first (`shares.py`): `{shares: [{token, title, port, path, device, conversation_id, public, created_at, ends_at, state, line, url}]}`, `device` the device it goes through (null: the shellbox), `state` live or down from a probe of its box or device, `ends_at` when it ends (or ended), `line` its card's words (what is shared and from where while it runs, else Not running or Ended), `url` its open route |
| `GET /shares/{token}` | one share, `state` live, down or ended |
| `GET /shares/{token}/open` | sets the share's cookie and redirects (303) to `/share/<token>/`; a share that does not run is a 404 page |
| `POST /shares/{token}/stop` | ends the share for everyone; the share, `state` ended |
| `GET /browser` | its browser box's status (`browser/api.py`): `{state, reason, started, taken, screen, tabs}`, `state` starting, up or down from the box, `unreachable` when it does not answer |
| `POST /browser/control` | `{action}` take, give or restart → the box's `/control`: take and give answer `{taken}`, restart its status once it is up or down again (30 s at most) |
| `GET /browser/calls?q=&failed=&before=&limit=` | its browser calls (and what a server shows there, `place = "browser"`: the Mac's browser), newest first (the Browser place's History): `{calls, total, failed, next}` |
| `GET /events?tab=&kind=&day=&state=&decision=&rule=&mode=&before=&limit=` | Events, everything it did and everything that ran, newest first (`events.py`, Events above): `tab` guard, celine (the default) or background, `kind` one of that tab's; `{events, total, failed, undecided, next, tabs, kinds, days, guard}` |
| `POST /missions` | `{name?, model?, brief, report_to?, time_limit?, min_time?, check_in?}` (`brief`: its Instructions; `min_time`: its Minimum time; `check_in`: `30 min` or `2 h`, the check-ins' cadence) (any other key is a 400; `missions/api.py`) |
| `GET /missions?status=` | every mission, newest first, with its conversation's title (`missions/store.list`), and `counts` per state, archived and trashed (`missions/store.counts`) |
| `GET /sub-agents?conversation=` | every sub-agent (of one conversation, or all), newest first: `id, parent, name, access, folder, model, cli, working` (not `report.quiet`), `step` (the tool at work), `report` (the latest) |
| `GET /conversations/{cid}/sub-agents` | the tree under the conversation's top: `{top, agents}`, each agent `id, parent, depth, name, state, model, prompt_tokens, completion_tokens, seconds, link`, parents before their own |
| `POST /conversations/{cid}/sub-agents` | `{brief, name, access?, folder?, model?, cli?}` (any other key is a 400): a sub-agent of the conversation, exactly as `sub_agents__start` makes one (`skill.begin`); `{conversation_id}` |
| `POST /sub-agents/{id}/stop` | its turn, its jobs and what it scheduled, as `sub_agents__stop` (`skill.stop_agent`); `{stopped}` |
| `PATCH /missions/{cid}` | `{name?, brief?, report_to?, time_limit?, min_time?, check_in?, steps?}`, the given fields only (any other key is a 400); the time limit's notice cancelled and scheduled again or removed |
| `POST /intercom` | `{to, text, subject?, reply_to?, in?, at?, until?}` (`intercom/api.py`): from `user` with the app session, from `system:<name>` with a named token |
| `GET /intercom?to=&from=&thread=&scheduled=&waiting=&after=&limit=` | messages to and from stored addresses, in a thread, above id `after`, oldest first, each with its `state` (pending, sent, cancelled) and `unread`; `scheduled=true`; `waiting=true` only those that wait for you |
| `GET /intercom/threads?with=&kind=&unread=&limit=&cursor=` | threads grouped by who they are with, by name (all its chats are one group), the newest activity first (`ThreadsPage`): `{groups: [{name, addresses, threads, waiting, items, more}], jobs: {count, newest}, next}` |
| `POST /intercom/read` | `{ids}`: the user's deliveries of these become `read`; `?all=1`: everything that waits for the user (Dismiss all); `{read}` |
| `POST /intercom/threads/{id}/conversations` | Continue in a chat: a new chat with it (titled by the thread's subject) that carries the thread, its messages landed as one `notice` from `system:app` without waking it |
| `GET /intercom/stream?to=&after=` | server-sent events, `id:` the message id: every message above `after` (or `Last-Event-ID`), then each new one as it is sent (woken by the send, not by the bus); a comment line every 15 s |
| `POST /intercom/{id}/stop` | cancels a scheduled message that has not landed; `{ok}`; 404 `This message was already delivered or cancelled.` |
| `GET /tokens`, `POST /tokens`, `POST /tokens/{id}/revoke` | named tokens (Settings, Security): the list without hashes; `{name}` makes one, `{name, token}` shows it once; revoke hides it, the row stays |
| `GET /push/key` | the VAPID public key a browser subscribes with (`push.py`): `{key}` |
| `GET /push-subscriptions` | the devices that take pushes, oldest first (Notifications, Push): `{subscriptions: [{id, device, endpoint, kinds, created_at}]}`, `device` named by the server from the user agent; ended sessions' devices are dropped first |
| `POST /push-subscriptions` | `{endpoint, keys: {p256dh, auth}, kinds}`, a browser's PushSubscription and the notice kinds it takes: the device takes pushes (the same endpoint again replaces its keys and kinds), tied to the login session that made it; 400 `invalid` for anything else; one subscription as in the list; global event `push.subscriptions` |
| `DELETE /push-subscriptions/{id}` | Remove: the device takes no more pushes; `{deleted: true}`, 404 for an unknown id; global event `push.subscriptions` |
| `POST /missions/{cid}/stop` | status `stopped`, the time limit's notice cancelled, the conversation aborted (`abort_conversation`); `{ok}` |

**Slash commands** (`commands/`, a small registry: `command(name, description, args=,
aliases=, when=, kinds=)` adds one, the commands are in `commands/handlers.py`, the
routes in `api/commands.py`): `/compact [focus]` (summarise now), `/think [level]` and its
alias `/effort`, `/model [name|provider/model|default]` and `/usage`. A command is never
sent to the model as text: `POST /messages` refuses a text that starts with a command name
with 400 `is_command`; `//compact` sends the literal text "/compact".

`retry`, `edit`, `version` and message delete return `409 busy` while a turn of the
conversation is live. `retry` and `edit` return `409 unresolved_tool_calls` while any
tool call in the target subtree is `pending` or `running`.

One WebSocket, `GET /ws/events` (`api/ws.py`), for everything a page follows: any number
of conversations and the global feed. Auth like every route (the session cookie or a
named token, `auth/gate.py`). The client sends, at any time:

- `{"type": "subscribe", "conversation": id, "after": seq}`: `after` 0 sends a
  `snapshot` first (the row is its `conversation`, with `id`); else the events after
  `seq` are replayed. Subscribe comes before the read, so nothing is lost between them.
  Below `trim_below` the answer is `{"type": "resync", "conversation", "reason"}` and
  the subscription ends (subscribe again with `after` 0). An unknown id answers
  `{"type": "error", "conversation", "code": "not_found"}`. Subscribing again replaces
  the subscription.
- `{"type": "unsubscribe", "conversation": id}`.
- `{"type": "global", "after": id}`: the feed's rows after `id`, then live; without
  `after`, `{"type": "global", "gid": <newest>}` first and live from there. Every global
  frame carries `gid`, the feed's own number (an event's own `id`, a conversation's, is
  untouched). Below the
  oldest kept row: `{"type": "resync", "feed": "global"}` and that subscription ends
  (refetch, `GET /api/changes`, and follow from now).
- `{"type": "ping"}`, answered `{"type": "pong"}`.

Frames are the events as they were written: a conversation's `{seq, type, ts, ...data,
conversation}`, the feed's `{gid, type, ...data}`. Replay merges consecutive
`message.delta` frames of one message into one; live, `tool.progress` goes out at most
every 250 ms per call. A reader the bus drops as too slow is caught up from the log by the
server, never left with a gap.

Conversation events: `snapshot`, `turn.state`, `message.created`, `message.delta`,
`message.reasoning`, `message.done` (once, with its text), `message.rewritten` (once, when
the text changed after that), `message.purged`, `message.reopened`, `message.attachments`,
`tool.forming`, `tool.started`, `tool.progress`, `tool.done`, `tool.gated`, `tool.detached`,
`tool.reset`, `job.state`, `plan.state`, `guard.banner`, `approval.asked`, `approval.closed`, `images.batch`,
`music.batch`, `version.changed`,
`overrides.changed`, `llm.queue`, `context.*` (see Context), `conversation.settings`,
`citations.checked`, `terminal.state`, `error`. `turn.state` carries the conversation's live
`queue`, `message.created` the message's `meta`.

The global feed carries what every open page needs across conversations: turn and job
states, titles, the archive's tasks and sources, the model queue, settings changes, and
bare signals such as `terminal.state` and `browser.state`; `conversation.created`,
`conversation.deleted`, `conversation.restored`, `conversation.pinned`,
`conversation.archived`, `conversation.unarchived`; `agent.state`, `mission.state`,
`intercom.state`, `artifact.updated`, `images.batch` and `music.batch`, `events.added` (a row of Events
landed), `notice` (one item for a system notification while the tab is hidden: its
reply in a chat, an Intercom message to you, a mission waiting for you, a failed run, a
request to approve; `{kind, title, body, link, tag}`, events.py `notice`, web
lib/notify.ts), `mood`, `agent.changed`, `agent.hold`, `frozen.changed`, `guard.passes`,
`approvals.changed`, `waits.changed`, `run.state`, `share.updated`, `push.subscriptions`,
`models.state`, `llm.queue` and `kb.*`. The agent's shell and browser calls' `tool.started`,
`tool.done` and `tool.detached` pass through it as ids.

`GET /api/changes?since=<global id>&conversations=<id:seq,...>` is one call for a client
coming back: `{global, resync, conversations, gone, jobs, agents}`: the newest
global id, whether `since` is below what the feed keeps, the given conversations whose seq
moved, the ids that no longer exist, the jobs that finished since, and every sub-agent as
`GET /sub-agents` gives it.

uvicorn runs with `ws_max_size` 1 MB (a client sends only control frames) and a ping
every 20 s (`ws_ping_interval`, `ws_ping_timeout`).

WebSocket `GET /ws/terminals/{id}` (`terminals/api.py`) is a terminal's bytes as they
come: the daemon's `GET /view?terminal=<name>` relayed, every chunk as one binary frame,
then `{"type": "end"}` when the terminal ends. Binary frames from the page are keys into
your own terminal (the daemon's `POST /keys`); keys into one of its own close the socket with
1008.

WebSocket `GET /ws/browser` (`browser/api.py`) is the browser box's screen: a TCP
connection to its Xvnc relayed both ways as binary frames for noVNC. The page's input
reaches the screen only while you have taken the browser.

The agent's terminal history (`terminals/sync.py`, `terminals/store.py`): every sync round appends
the daemon's bytes after the stored size to `<data dir>/terminals/<id>.raw`, every known
secret blanked to stars; an ended terminal is gzipped to `<id>.raw.gz`. Nothing is ever
deleted. The terminal page writes the history into xterm first, then attaches the live
view.

## Web

The layout, the component kit and every rule of the UI are in docs/ui/.
In short: a rail (the places of `web/src/routes.tsx` in order; Settings at the bottom; under
768 px a bottom bar of icons), and one section at a time. Sections mount on first visit and
stay mounted, so a running answer, a scroll position or a draft survive switching. Tokens
are in `styles/tokens.css`. The app installs as a PWA (`web/public/manifest.webmanifest`).
Its service worker (`web/public/sw.js`, registered by `lib/notify.ts`) shows the server's
web pushes (`push.py`: each `notice` goes to every device subscribed through
`/api/push-subscriptions` while no page of the app is open and focused) and the page's own
notices as system notifications; it has no fetch handler, so nothing is cached.

One socket for the app (`lib/socket.ts`, `WS /ws/events`): the global feed (every page listens
through `lib/bus`) and a subscription per conversation on screen (`lib/useConversation.ts`,
`follow`), plus a terminal page's own (`/ws/terminals/{id}`) and the browser page's
(`/ws/browser`). A frame with `gid` is the feed's; any other with `conversation` goes to
that conversation. A conversation drops frames at or below its last `seq`, takes a fresh
snapshot (subscribe after 0) on `resync`, `version.changed` and `message.purged`, and
applies `message.rewritten` like `message.done`. After a reconnect
the client calls `/api/changes` once with its last `gid` and its conversations, follows the
feed after that gid, takes a snapshot of each conversation whose seq moved, and puts the
result on the bus as `changes`. Following the feed from now (the first connect, a resync)
sends `open` on the bus: the pages fetch again what they show. Only `/api/health` decides
that the server is gone (`lib/health.ts`).

The agent's place: the conversation list (pinned, then by date, paged with the list's
cursor; each row's preview is the row's `last_line`), the transcript
(`components/Transcript.tsx` on the kit's Message, with the message actions of
`components/MessageActions.tsx`), the composer (`components/Composer.tsx`: draft per
conversation, attachments, slash commands, Impersonate) and the side panel (Model, Tools,
Pages, Context, Sub-agents). `useConversation` keeps the snapshot's `queue` up to date from `turn.state`
so the page never reads idle while a queued turn waits; `tool.started` lands on its
`message_id`. The side panel's Background jobs list reads `GET /jobs?conversation=`,
reloaded on that conversation's `job.state` events. The notebook (`components/Notebook.tsx`,
its own place at `/notebook`) edits its files through `/api/notebook/*`; paths are confined
to its `notebook/`, protected files are read-only.

## Operations

`celine doctor` loads the config and prints one line per check (problems, the stored
address, database and the size of each file, providers, knowledge bases, skills, MCP
servers, triggers). It reads only, so it runs beside the app and on a database from an
older release. Exit 1 if anything is marked WARN.

`celine export [--out DIR]` writes `conversations.json`, one Markdown file per conversation
with its active path, the blob files, and a copy of the notebook (default
`exports/export-<utc time>` in the Documents folder).

## Logging and secrets

`var/logs/celine.log`, JSON lines, one line per event with ids, names, durations and
outcomes. Message content, notebook content, tool arguments and tool output are never
logged. One redaction set holds every resolved secret: the config's and Settings' keys
(`Config.secrets()`) and every password in its vault (`vault/`, from the unlock on).
`errors.redact` masks the set plus common key shapes, applied in the formatter and in an
error's text. `errors.mask` masks the set only: on its tool calls' arguments before they
are stored or run, on every tool result as the runner receives it, and on its text and
reasoning as they are stored. `errors.blank` turns each byte of a secret into a star in its
stored terminal history. No turn starts before the vault's first login is over (the
dispatcher's `ready`, 15 seconds at most). The live stream to an open page is not masked.
`tests/test_redact.py` and `tests/test_vault.py` cover it.

## Dev loop

`./start.sh dev` runs the API with `--dev` and Vite with a proxy for `/api` (`ws: true`).
`./start.sh` rebuilds the bundle into `server/celine/static/` when `web/src` is newer,
then serves everything from `:8766`. `uv run pytest` runs the suite on fakes (no model
server, no network); `CELINE_LIVE=http://127.0.0.1:11434/v1 uv run pytest -m live` runs
3 tests against a real server. Gates, tests, instances and screenshots: docs/dev.md.
