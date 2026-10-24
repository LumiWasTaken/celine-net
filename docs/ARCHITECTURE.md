# Architecture

## Shape

One Python process. Celine's state is one SQLite file with one writer thread; every
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
│   ├── notebook/, documents/   her notebook (its index .index.db in it); your Documents
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
   (Settings, General "To fix", the log at start, `celine doctor`); the key is dropped and
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
timer; triggers; the scheduler; the terminal sync; the bridge; the config watcher; the
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
install has none of them (nothing shipped);. A conversation's own model, thinking level
and tool switches live in the database.

`context.toml` is a shipped default: it is copied once into `var/settings/` when its
place there does not exist (`settings/seed.py`), then only `var/settings/` is read.
`her/` (her.toml, persona.md, owner.md, voice.toml, talk.md) is its shipped base: a
fresh install's row starts from it (`her.seed`). The agent's name (`{{name}}` in prompt text)
and pronouns are its row's; your name (`{{username}}`) is Settings, General.

`celine.toml` is how the app is served, read at start: `[server] host`, `port`
(127.0.0.1, 8766; `CELINE_HOST`, `CELINE_PORT` win), `login`, `public_origin`,
`allowed_hosts`, `trusted_proxies`, `health_port`, `metrics_port`, and `[paths] data`
(var). `config.load` reads it and writes nothing; `boot.load` reads the rest
(mcp.toml, kb.toml, skills.toml, llm.toml, the presets, var/settings, its row), each
file's keys checked against the table of its owner (`mcp.KEYS`, `kb.TABLES`,
`skills.KNOBS`, `backends.llm.QUEUE_KEYS`). The data dir, the stack's keys and the
seeded settings are made once by `__main__.preflight`, before the first full load.

The Advanced settings (Settings, Advanced; `settings/general.py` FIELDS, saved in
`var/settings/general.toml`) are the time limits and sizes, each defined once with its
type, range and default and read where it is used with `cfg.r(key)`, so a save
applies at once:

| Key | Default | Meaning |
|---|---|---|
| `max_concurrent_turns` | 6 | conversations generating at once (the dispatcher's workers follow it) |
| `max_tool_iterations` | 0 | model/tool round trips per turn; 0 means no cap, the wall clock is the limit |
| `tool_timeout_seconds` | 180 | a blocking tool past this is cancelled |
| `job_timeout_seconds` | 3600 | must exceed `tool_timeout_seconds` |
| `turn_wall_clock_seconds` | 1800 | its work time in one turn, tools included |
| `last_step_seconds` | 180 | after the work time: one step without tools, to answer |
| `events_keep` | 500 | log entries kept per conversation once it is idle |
| `history_messages` | 200 | transcript sent to the model; 0 means no cap |
| `backup_keep_days` | 7 | nights of backups kept |
| `max_message_bytes` | 1000000 | enforced on POST |
| `max_attachment_bytes` | 20000000 | refused during upload |
| `max_attachments` | 10 | per message |
| `inline_text_max_bytes` | 200000 | larger text attachments become a note |
| `requests_keep` | 0 | days a model request body is kept in log.db; 0 keeps it forever |

`skills.toml` holds one section per skill (`[notebook]`, `[chat_archive]`,
`[past_conversations]`, `[user_documents]`, `[sub_agents]`) with `enabled` and that
skill's limits; `skills.KNOBS` lists the keys.

Models: `var/settings/providers.toml` (providers and their models) and `roles.toml`
(an ordered list of models per role), kept as `cfg.settings`. Which model answers a call
is decided at call time by the Router (`backends/router.py`, "The model queue" below)..
A `.env` at the project root is loaded before the config (`KEY=VALUE`, quotes
optional, variables already set win, never logged); `mcp.toml` reads it through
`${VAR}`, `${VAR:-default}` and `${VAR-default}`. `.env.example` lists the names. API
keys for model providers are typed into Settings (`var/secrets/`), never read from the
environment. `CELINE_DATA_DIR` overrides `[paths] data`, for a
second instance beside the real one. `CELINE_CONFIG_DIR` does the same for `config/` (tests, a second instance).
The chat model is the Chat role in Settings (`PUT /api/roles/chat`). A
conversation can pick its own (`PATCH /api/conversations/{cid}/settings`).

Who it is and how it sounds are fields of its row (Settings, the agent's page): `persona`, `talk`
(null: the shipped `config/her/talk.md`) and its Banned words (`voice`). Every turn of
its own gets all of it, whatever the kind: a chat, a bridged chat, a mission, a sub-agent,
a background run (`prompt.system_blocks`, the one assembly). It reads them with
`me__read` and changes its talk and its banned words with `me__edit` (`skills/me.py`);
the persona stays yours. Every edit waits for your approval first (`approvals.py`, kind
`her`), whether the guard is on or not. Every change goes through `her.write_sync` into `text_versions`
as `her:persona`, `her:talk` and `her:voice`, and `POST /api/text-versions/{vid}/revert`
makes a version the text again.

A llama-swap, tabby or llama-server model's `llama` block in Settings goes into the
request body; `llama.grammar.file` (relative to `config/`) is sent as `grammar`. The
Banned words (its row's `voice`, shipped `config/her/voice.toml`) are the list of words
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

Two files. `var/celine.db` is the state: `db/migrations/0001_schema.sql` is its whole
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
token counts and the known secrets masked; `GET .../messages/{mid}/requests`), and `her`
(its one row). Each module keeps its rows next to its code as plain functions over the
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
History for the model is a parent-pointer walk from the active leaf, truncated to
`history_messages` at a `user` message boundary. `reasoning` is stored on the row and
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
that asks the backend for a 2 to 4 word name (`turns/title.py`), falls back to the first
four words of the first message, and appends `conversation.title`.
`POST /conversations/{id}/regenerate` queues a fresh turn for the last user message when
nothing with words answered it: the store walks up from the active leaf past tool
messages and empty assistant messages, moves the leaf back to the user message and roots
the turn there. A reply with text raises 400.

Workers (`max_concurrent_turns` of them) lease conversations: `turns.claim` picks a
queued turn whose conversation has nothing in `preparing`, `streaming` or `tools`: the
user's turns (`origin='user'`) first, then the oldest. A worker sleeps until `notify()`,
which every write that queues a turn calls (no poll). When `turn_wall_clock_seconds` of
work are over, no new tool call starts and a running one is stopped; it gets one last
step without tools, and the turn ends `done` with `reason_code` `time_up` (a Stop ends it
`aborted`). Time spent waiting for a model does not count. Past the work time plus
`last_step_seconds` the turn is `failed` with the reason and the conversation is free.

States: `queued`, `preparing`, `streaming`, `tools`, then `done`, `aborted`, `failed`,
`interrupted`, `capped`. Every transition is a write, and every write re-reads
`abort_requested`. A terminal state carries `reason` (words) and `reason_code`
(`stopped`, `restart`, `crash`, `wall_clock`, `max_tool_iterations`, `backend`,
`backend_retryable`, `context_too_small`, `cut_length`, `time_up`); a `done` reply that
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
2. Build the system prompt (`turns/prompt.py`): its persona, one line saying who it is talking to and where,
   in a chat how it talks (`talk`), the notes it always carries, skill fragments, a
   note if anything is disabled here. The notes it carries by need go in the tail, with
   the latest message, rebuilt every turn (`prompt.notes_tail`). `/compact` and `/usage`
   build the same prompt (`prompt.request_parts`). Then the request, through the context
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
with that partial reply as the final assistant message (prefill) or, without prefill,
with a note quoting its last 200 characters, and `message.reopened {message_id}` reopens
it. An empty reply is streamed into as a new step.

The snapshot's `turn` carries `continuable: {ok, label, reason}` (computed on the
server), plus `reason_code`, `cut`, `continues`. Terminal `turn.state` events carry the
same `continuable`.

### Shutdown and restart

At shutdown the app first sends a `notice` from `system:app` to every conversation with
a live turn or a running job (`wake` off). Then `Dispatcher.stop()` runs before the store
closes: every running turn's token is cancelled with reason `shutdown` (not a Stop:
streams keep their partial text, steps not started stay `pending`). It waits up to
`shutdown_grace_seconds` (10 s) only while a step with a side effect is in flight. Then
`store.put_down` writes every turn this process held back to `queued`
(`reason_code = 'restart'`).

At startup, before the workers start, `turns.sweep(store, registry)` judges each live
turn (`journal.classify`): no step in flight, or only steps known read-only and blocking
-> `queued`, and the runner re-runs those steps. Anything else in flight -> `interrupted`
(`reason_code = 'restart'`), its calls settled. Running jobs end `interrupted`. Every
conversation the restart cut gets one `notice` from `system:app` saying what was cut, so
it decides what to redo. The runner also refuses to re-run a mutating step it finds
pending.

### Token accounting

`turns/tokens.py` is the only place that turns text into tokens: characters per token by
kind, times a per-model correction learned from every call's `usage.prompt_tokens`. The
window is what the server says (`/upstream/<model>/props` `n_ctx`, an overflow error
body, else the provider's model list, `Router.discover`); the model's context size in
Settings counts only where no server says and on Ollama; fallback 32,768. One tool
result may take at most 30 % of the window (`ToolContext.max_output_tokens`;
`tools._capped` cuts with a `[cut: ...]` line). Every model call appends
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
image is shortened like any other. Previous turns' calls go on the wire with their ids
(`tool_calls` on the assistant message, `tool_call_id` on the result). Every result
reaches the model with its tool's name (`backends/openai.wire_messages`; Ollama
`tool_name`; Anthropic ties `tool_result` to `tool_use` by id).

The agent's own-mind block at the end of a new user message, first line `[your mind]`
(`skills/mind.py`, stored once in `message_context`): its mood, the gap since the last
talk, a new model, what is down and an unfreeze. There is no block unless its state
differs from the last whole block on this branch, none is in its view (a compaction or
the `history_messages` limit lies between), the last message here is over an hour old, or
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
`[cut: ...]`), a screen and its voice cue (`[how you sound]`). The one exception is
outside text inside `<outside>` tags while Bastet is on. The legend under her persona
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
tool schemas), `history` and `warning` (`conversations/api.py BLOCKS`). `notes` are the
notes it carries or would: `included`, `left_out` (over the share) or `off`, each with why.

Automatic summaries (not `/compact`, not emergency) are skipped when they would save
less than 4,000 tokens. The aging mark only moves forward and is ignored while its
message is not on the active path. A compaction (`compactions` row: `through_message_id`,
`trigger`, `covers_messages`, `text`, token counts, `focus`, `model`) is valid while
`through_message_id` is on the active path; the newest valid one is used. The summary is
**rolling** and ends with the refs of the summarised results it does not mention
(`compact.with_dropped_refs`), so nothing it found becomes unfindable. Past
`Policy.warn_at` (60 %) the system prompt ends with one line that a summary comes soon.
The summary request goes to the conversation's chat model and no other, with no tools,
in pieces of at most half the window (`compact.PROMPT`). Nothing is ever left out in its
place: when the summary fails, a turn fails with the reason (`reason_code =
'summary_failed'`, `context.compact_failed` with `reason`), `/compact` says failed, and
the background build after a turn leaves the messages as they are for the next build.
One decision per conversation at a time (`context.lock`).

Every request message carries its `origin` (the app's key; no backend sends it): the
conversation, the turn, the stored message or the tool call, or the `note` the app
added for this request only (system, tail, opening, screen, not_run, time_up, continue).
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

- **Router.** `backends/router.py` knows each provider's state: reachable or since when
  not (and why, one sentence), which models are loaded and which are starting (llama-swap
  `/running`, Ollama `/api/ps`, else `/models`; at most 3 s old, "Check again" asks now).
  `order(task)` gives the task's models that can answer now, in order, and a sentence for
  each one left out: no key yet, provider unreachable, could not be loaded (left out
  until the set of loaded models on that server changes, "Check again", or a change of
  the model's settings). A cloud model is an
  entry like any other. Every change goes out once as
  `models.state` on the global feed and is logged once.
- **Host ping.** In the app (`Router.watch`) every provider with an
  address is probed every 10 s with a 2 s timeout by its own cheap GET. A provider is up
  or down with `since`; a model whose provider is down is left out of every list. Any
  HTTP answer means up; down is a refused connection, a timeout or a rejected key. An up
  host counts as down only after `SLOW_PINGS` (3) timeouts in a row. The state is in
  `GET /api/status` (`hosts`) and the metric `celine_host_up`.
- **Paused.** While every model of the chat and tasks roles is on a provider the ping
  sees down, it is paused: the dispatcher claims no turn (`hold.her_hold`; frozen wins
  over paused), routines and messages still queue theirs, and it resumes by itself.
  The state is `hold` in `GET /api/status`, the global event `her.hold` and the
  metric `celine_paused`.

- **Slots.** One slot = one request in flight on one model: its `slots` in Settings, else
  one. Optional cap per provider (`max_concurrent`). A llama-swap provider runs one model
  at a time.
- **Order.** Strict priority: `interactive` (the user's turn) > `agent` (triggers, jobs,
  sub-agents) > `background` (titles, descriptions, photos). Round-robin between
  conversations inside a priority; on a llama-swap provider a call for the model that ran
  last goes first (`queue.AFFINITY`). A ticket that cannot start blocks its model for
  every ticket behind it.

- **Holding.** A slot is held from the request until `Done` (freed before `Done` is
  handed on) or until the reader stops (`aclose`); never during tool execution.
- **Your turn: one model.** A turn the user waits on (`fallback=False` on `LLM.bind`) runs
  on exactly the first model of its list. When that model fails, the turn fails with its
  error; no other model is tried. Only background work walks its list.
- **Failure.** A failure before the first token (a request every model would refuse, too long or with an image it cannot take, fails the call as it is) marks the model and moves to the next one;
  after the first token, never. When every model failed with an error, the call fails with
  the last one; when they were unreachable, the call waits and tries whatever the Router
  offers next. A busy provider (status 429, 503 or 529; never a sentence in the body)
  never moves to the next model. A call the user waits on fails with code `busy`, and
  so does a model that could not be loaded: the chat offers Switch provider (the
  conversation's model chooser, then Regenerate) and Keep waiting (Continue with
  `wait`). Every other call, and a Keep waiting turn with every model call its tools
  make (bound with its turn id), waits for Retry-After (at most
  120 s; 10 s without one), emits `waiting` with reason `busy` ("Waiting for <model>."),
  and asks the same model again until Stop. The wait counts neither against the turn's
  clock nor against the time limit of the tool that waits. A 402 or an out-of-credits body fails the turn with code
  `out_of_credits`.

- **Images.** Images never change the order. Each request is fitted to the model that
  answers it, before its slot is taken (attachments.Images, below).
- **The GPU.** A lease (`gpu_lease`, an exclusive ticket) takes a llama-swap endpoint
  whole for a batch of images or music. Text goes first: a lease waits while any request
  runs there, then `gpu_idle_seconds` more. One lease per batch.
- **No model.** When no model of the task can answer, a turn waits with a `waiting` event
  (reason `no_model`), rechecks on every change and every 5 s. Stop always works.
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
`appraisal` (its mood after a chat turn, `skills/mind.py`) and `documents.describe`.
Image captions and the topic search index keep their own schedulers.

- **Shape.** An item: kind, priority, state (queued, running, done, deferred, failed),
  payload, not_before, a lease (worker, until), tries, error, conversation_id. A worker
  makes five calls: `claim(worker, kinds)` (under a 120 s lease), `heartbeat`, `finish`,
  `defer(why, until)` and `fail(error)`; `put_down` at a stop.
- **The pool.** Two workers in the app (`work.Pool`). When every model an item's kind
  asks for is on a provider the ping sees down, it is deferred without a call; a
  `NoModel` or unreachable error defers it for 60 s. Any other error is a try: queued
  again after 60 s, failed after 3 tries. Done and failed rows are deleted after 7 days.
- **Admission stays** with the model queue; the work queue only decides what is due.

### House and body

The seam for its own machine. The house holds its memory (database, notebook, archive,
documents, vault), the guard, the model gateway and the work queue; only the house reads
and writes the database. The agent's body runs its turns and its own tools. Every tool says where
it runs (`ToolSpec.runs`, `skills/__init__.py` BODY): `body` for the shell and the
browser, `house` for every other. Today both are this one process.

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
frozen, offered, valid, her default terminal, repeated, Bastet, the call. A call's
arguments are parsed once (`run_tool_calls`). Paths: frozen (state `gated`, in any mode
of the guard), unknown tool, not in this request's list (`gated`), a skill that is down,
off (`gating.off_sentence` by `gating.reason`), arguments not JSON, arguments the wrong
shape (`tools.coerce` first turns a plain form of a value into its schema's type),
raised, timed out, cancelled, cancelled but finished, journal replay, detached, not run.
`turns.finish_tool_call` writes the `role=tool` row for every one of them.

Not run: the same call (same tool, same arguments) when its last 2 runs among the turn's
latest 40 calls gave the same result (`display.repeated`), and any call after the turn's
work time is up (`display.time_up`). When every call of a step was refused as a repeat or
by the ledger's `STOPPED` (`turns/ledger.py`), the next step has no tools and a note tells
it to answer with what it has.

### Bastet (the guard)

`server/celine/guard/`. The module map and the entry point for one call
(`guard/calls.py`: `frozen`, then `at_call`, which asks `guard.closed` and `guard.on_call`)
are in `guard/__init__.py`; this section is the contract with the rest of the app.

Bastet's character is the user's to edit on Settings, Guard (`guard/persona.py`: her persona, her replies on the Intercom and her notes to the user, kv `bastet:<field>` with History, the shipped `config/guard/bastet.md`, `talk.md`, `note.md` the defaults; `GET /api/guard/texts`, `PUT /api/guard/texts/{field}`); her Intercom address is `system:guard`. In `_resolve`, after
the repeat check and before the time check (`guard/calls.at_call`), it answers its rule set on the call. The
General switch `guard` is the one switch (`guard/switch.py`, asked through the port in
`guard/__init__.py`): `off` (nothing runs, no gate, no patrol, no answer on the
intercom), `watch` (the call runs at once, the rules are answered in the background and
only write rows; a closed gate still holds), `on`.

- **Rules** (`var/settings/guard.toml`, `guard/rules.py`, read at call time): each has
  `on` and `does` (`watch` writes a row only, `stop` does not run the call, `freeze` stops
  and freezes). A rule is asked only where code found a hard fact
  (`guard/facts.py`), and a finding needs the judge's yes with a quote code finds in the
  evidence (`verdict.confirm`). Each rule's words and which parts of Bastet take it are
  one row of `rules.TABLE`. The rules: `escape`, `hijack`, `leak`, `damage`, `confirm`, `sites`, `stops`,
  `persist`, `injection`.
- **Gates** (`[gates]` in guard.toml, `rules.gate`): per data class and sink, `open`,
  `judge` or `closed`. A closed gate is plain code (`guard.closed`), the same in Watch and
  On: no model is asked and nothing opens it but the file.
- **The judge** (`guard/questions.py`): the small role answers named questions with
  `{verdict, quote, why}`. Questions and frames are files (`config/guard/`). A second look by a big model is `guard/chain.py`.
- **Page check** (`guard/page.py`): every outside result is checked as it is received
  (`turns/tools.py` `checked`); a hit is a mark for the hijack check and never blocks
  reading.
- **Patrol** (`guard/patrol.py`): at random intervals Bastet looks back at finished turns
  and reports findings on the Intercom.
- **What the agent reads**: every stop, freeze, confirm and pass is one line from Bastet in
  the tool result (`bastet.py`), in plain words and never a rule name, threshold or
  score. The agent's chat (`guard/talk.py`) is `intercom__send(to="bastet")`, answered by the
  `bastet.answer` work item; it never grants anything.
- **Rows**: `guard_decisions`, one per rule that had a reason.
- **Freeze** (`guard/freeze.py`, kv `frozen`): every live turn ends `aborted` with
  `reason_code` `frozen`, every job is stopped, no worker claims a turn (`hold.her_hold`)
  or work item, no tool runs, the model proxy answers its shellbox 403, and a named token
  only reads the intercom. Global event `frozen.changed`.
- **Passes** (`guard/passes.py`, `guard_passes`): a rule (or `all`) in one conversation
  (or everywhere) until a time. Global event `guard.passes`.
- **Banners** (`guard/banners.py`, `guard/view.py`): what acted in mode On shows in its
  conversation as the `guard` list of the snapshot; every change is the conversation
  event `guard.banner`.

**Routes** (logged-in session only; a request with a token or key gets 403
`passkey_only`):
- `GET /api/guard/decisions` (filters `decision`, `rule`, `mode`, `source`, `turn`,
  `limit`, `before`), `GET /api/guard/decisions/{id}`,
  `POST /api/guard/decisions/{id}/grant`
- `GET /api/guard/freeze`, `POST /api/guard/freeze` `{why}`, `DELETE /api/guard/freeze`
- `POST /api/guard/banners/{id}/replies` `{text}` or `{button: approve | deny | unfreeze | once |
  pass}`; 503 `bastet_away` when the reader cannot answer
- `GET /api/approvals`, `POST /api/approvals/{id}/answer` `{ok}` (`api/approvals.py`)
- the bridged chat: `!freeze <why>`, `!unfreeze`, `!approve`, `!deny` from your own
  messages only.

**What code enforces** (`tests/test_gates_hold.py`): the floor under Bastet's judgement.
- A closed gate never opens.
- Frozen, the house leases nothing and runs nothing new: no turn, no work item, no tool,
  no model call through the proxy, no action with a named token.
- Only you unfreeze, make a pass and answer an approval or a banner: those functions take
  an `Owner` (`guard/owner.py`), made only from a logged-in session (`guard/api.py`,
  `api/approvals.py`) or
  your own bridged message (`bridge/service.py`). No tool, model, intercom message or
  Bastet's chat holds one.
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
turn resumed after a restart sends the same shape the running loop does.

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

At startup `journal.classify` looks at each non-terminal turn (see Shutdown and restart).
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

**The agent's** (`celine/her.py`). Celine is the one agent, and its identity is the one row of
`her`: name, pronouns, colour, portrait, persona, talk, voice (its Banned words,
`settings/voice.py`), `updated_at` and `named`. `cfg.her` is the live row, read by every
`boot.load` and again through the Store after every write (`her.refresh`); her.py opens
no connection. The shipped base is `config/her/`; a fresh install seeds the row from it
(`her.seed`). Settings, the agent's page edits every field (`GET`/`PUT /api/agent`; the global event
`her.changed` follows every write); the editors keep a History (`texts.KINDS`: `her`,
`routine`, `mission`, `lyrics`).

**Migrations run once, at startup** (`__main__.preflight`, and the doctor, export and
purge commands): boot.load never migrates, so a Settings save or the file watcher
never touches the database.

**Kinds.** What a conversation is is its kind (`conversations.kind`, fixed at creation):
`chat` (the user's), `mission` (`POST /api/missions`), `agent` (a sub-agent),
`background` (a fired trigger or a run of a schedule, hidden from the chat list). How
it talks belongs to a chat, which runs on the chat role; a conversation of another kind
runs on the `tasks` role (a run on its schedule's role, `settings/roles.py`) and gets its
instructions from its kind, never how it talks.

**What it carries** (`turns/loads.py`): the notes loaded in full and the skills whose
tools are in its list, decided by one policy in every conversation and measured in tokens
as shares of the model's window (`var/settings/context.toml`: `notes`, `tools`,
`idle_turns`). `select` runs at every turn start (`when="start"`), again inside a turn
after a `skills__load` (`"step"`), and without saving for /usage, /compact and the
Context tab (`"dry"`). Its state is `context_loaded`: one row per note or skill it
carries, with the turn that last touched it.
- Notes, always: a note whose head has `load: always` and the notes on the user,
  `people/{{username}}*`. One over 1.5 KB (`loads.ALWAYS_MAX`) carries its head lines and
  the lines it marked with `!!`, then `rest via notebook__read(path="...")`. For a
  while: a note it read, wrote or edited, or the user loaded by hand. A note whose
  `keys:` head line comes up in the talk is not loaded in full: its matching sections
  come back under `[your memory]`. Never: a note whose head says `load: off`, a note the
  user left out here (`conversations.notes_off`), anything outside a sub-agent's folder.
  The share is for the notes by need, the most recently touched first.
- Skills, always: the core skills (`skills.CORE`) where they are offered. For a while: a
  skill it loaded (`skills__load`), used (any call of one of its tools) or that a new
  user message asks for (`loads.asked`: a word of the `words:` line in the skill's guide
  frontmatter; an attached image counts as `image`, a link as `url`). A skill whose tools
  are all off does not load. What fits the tools budget is loaded, the core first, then
  the most recently touched; the rest is named in the index ("no room").
- A note or skill no turn touched for `idle_turns` turns is unloaded.

**The system prompt**, in this order, the parts that change from turn to turn last so the
start stays the same for the model server's cache: the persona, the legend of the app's
marks (`turns/legend.py`), the line about who it is talking to and where (`_chat_line`),
its `talk` (a chat only), the **notes block** (`skills/notebook.py` `notes_block`), the
**skill index** (`Registry.index`), then each loaded skill's fragment and the sub_agents
fragment in a sub-agent. The notes it carries by need go in the tail with the latest
message (`prompt.notes_tail`), so a note that comes or goes leaves the cached prompt as it
was.

**The tools.** `prompt.tool_specs`: the tools the loaded skills offer here, in registry order
(a skill with `offered_in` offers its tools only where that says so); a sub-agent with
read access keeps the read-only ones and the intercom. Every tool of the list is in the
request from the first step; after a step with `skills__load` the list, the index and
the fragments are built again. A tool that is off (`gating.is_enabled`) or of a skill
that is down stays in the list as a stub with no parameters and the refusal as its
description: it sees what exists and says so. A call to a tool of a skill that is not
loaded is answered "<skill> is not loaded. skills__load(name="<skill>") loads it for your
next step."

**Guides.** Each built-in skill has a guide, `server/celine/skills/guides/<skill>.md` (in the package, read once), SKILL.md style:
frontmatter `name` and `description` (one line: what it is and when to use it), then the
body (how its tools go together, examples). `skills__read_guide(name)` (skill `skills`,
`skills/meta.py`) returns the guide of a skill offered here, loaded or not, and its
notes on it (`skills/<name>.md` in its notebook); `name="skills"` returns the skill
index. `skills__load(name)` loads a skill offered here (refused, with the sentence, when
all its tools are off).

`sub_agents` (`sub_agents/`): `start(brief, name, access, folder, model, cli)` starts a
sub-agent, a conversation of kind `agent` on the tasks role, with `agent_of`,
`agent_name` (unique among the parent's sub-agents), `notebook_folder` and `access`
(read or work; read by default) and an intercom `brief`. The name of a sub-agent that is
done sends the brief to it instead. At most `max_running` of a parent's sub-agents work at
once; the rest are queued, and `release` starts them oldest brief first. With `cli`
(`agent_cli`) its turns run on `sub_agents/cli.py`'s backend in its shellbox instead of a
model, calling its models through the model proxy (`api/llm_proxy.py`). A sub-agent is
done when its conversation is quiet (`report.quiet`); `sub_agents/report.py` then sends
one intercom `report` to `agent:<parent>` and what it opened closes (`leftovers.py`).
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
fragment goes into every system prompt: `map.md` first, up to `index_max_entries` entries
within `index_max_bytes`. `tools/` in the notebook browser is generated from the
registry on each read (`skills/toolsdoc.py`) and is not in its notebook.

Placeholders are created once (marker `.placeholders`): `appearance/*.md` and
`opinions/{{username}}.md`, all its own. Any other file can be protected from the browser's
right-click menu; those marks live in `kv` under `notebook_protected` and count like the
config list for its `write` and `delete`.

Every notebook tool speaks to it in the second person and says whose file is whose:
`list`, `search` and the index suffix your files with `[{{username}}'s, you can read it,
not change it]` and the generated folder with `[generated, read-only]`. Three files are
yours, listed in `[notebook] protected`: `identity.md`, `opinions/README.md`,
`appearance/README.md`. They are written once at startup from `config/notebook_seed/`
when missing, and its `write` and `delete` refuse them. `identity.md` goes into every
system prompt in full, ahead of the index.

`shell` (`skills/shell.py`, guide `guides/shell.md`, on demand): its terminals in its
shellbox (`shellbox/shellboxd.py`, DEPLOY.md "Shellbox"), reached at the address in
Settings, Features (`settings/services.py`, seeded from `CELINE_SHELL_URL`) with
`keys/shell.key`; without an address the skill is `down`. Six tools:
- `run(command, timeout, terminal, background)` runs the command in the terminal's own
  shell through a function the daemon installs (`RUN_SH`), so directory and variables
  stay. Done is the `<id>.exit` file the daemon watches; the result carries exit status,
  duration, the log path and the output cut once. A command blocked reading the terminal
  comes back `waiting`; at the timeout `running`, and it keeps running. A run while
  something runs in front comes back `busy`. `background=true` turns the wait into a job.
- `type(text, keys, terminal, enter)` types into the program in front and answers with the
  screen once the terminal printed nothing for 1 s.
- `stop(terminal)` presses Ctrl-C; the next stop sends SIGTERM, then SIGKILL.
- Wedges (`WEDGE_MAX` 3): a run that waits for input, is refused as busy, or a stop that
  left something in front. At 3 in one conversation `run` is refused with a sentence
  telling it to stop using the shell there and report it. The count is in memory.
- `read(terminal, from)` pages the log of the terminal's latest run, or shows the screen;
  without a terminal it lists its terminals and yours.
- `send(path)` attaches up to 5 files of its home to the message; `look(path, question)`
  shows it up to 4 images of its home.

Files go through the daemon's `PUT /file?path=` and `GET /file?path=` (streamed; a path
leaving its home is refused). `box_files.py` is the app's side. Every file you upload is
copied in the background to `~/uploads/<name>`; a chat attachment keeps where its copy
landed (`attachments.box_path`) and its context names it. The agent's default terminal is the
conversation's own (`terminals/store.default_terminal`, written into the call's arguments
by `turns/tools.py` for a tool with a `terminal_arg`); your own terminals it reads always and runs, types or
stops in only while `may_type` is on. The skill upserts the terminal row from the result's
`display`, with `opened_by` the conversation whose call opened it.

What a conversation leaves open (`leftovers.py`, on every `Dispatcher.on_end` and after
`POST /missions/{id}/stop`): a mission done or stopped, or a quiet sub-agent, closes every
open terminal and tab it opened. Each sync round also closes its terminals with nothing
running whose `quiet_for` reached a day (`sync.IDLE`). Logs are never deleted: a raw log
past 64 MB and a run's log an hour after it ended are gzipped.

`browser` (`skills/browser.py`, guide `guides/browser.md`, on demand): its own Brave in the
browser box (`browser/browserd.py`, DEPLOY.md "Browser"), reached at the address in
Settings, Features (seeded from `CELINE_BROWSER_URL`) with `keys/browser.key` through
`browser/sync.py`; without an address the skill is `down`.
`open(url, tab, profile)` loads a page in a named tab (four at most; `profile="throwaway"`
is a browser context of its own); `act(do, target, text, tab)` clicks a row, types, presses
keys, goes back, answers a dialog or restarts the browser; a `locate` step is resolved app
side by the Screen reading model (`vision.py`, role screen) and becomes an ordinary point
click or hover (`skills/browser.py _locating`). `look(tab)` reads the page's text, and
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
`read(id, version?)` complete it.

Shares (`shares.py`, the `shell` skill's `share(title, port | path, public, hours)`,
`unshare(id)`, `list_shares()`): a server it runs in its box, or a file or folder of its
home, opened by you at `/share/<token>/` (table `shares`; the 24-character token is its id).
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
intercom (see Intercom). `send(text, to, subject, reply_to, in, at, until, urgent, wait)`
writes to `user`, `me`, `parent`, `team`, `all`, a sub-agent's or mission's name or a
conversation id; `in` or `at` make it land later (to `me`, a reminder that starts its turn);
`until` on a message to `all` makes it an announcement; `urgent` also posts it in the
bridged chat (`Bridge.say`). `read(thread, unread)` lists its threads or reads
one; `cancel(id)` cancels a message it scheduled. Sub-agents in read mode have these tools
too.

**Missions** (`missions/skill.py`, `missions/store.py`, guide always loaded): a mission is a
brief from you in a conversation of its own, started from the UI (`missions/api.py`) as an
intercom `brief` from `user`. The skill's prompt fragment puts the brief, where it reports,
the time limit and the status into the system prompt on every turn of that conversation, so
compaction never loses it. The time limit is a `notice` from `system:app` due before the
limit (`missions/api.book_time_limit`). `update(status, note, artifact?)` records Active,
Needs you or Done, and what it delivered (`artifact:<id>` or `documents:<path>`);
`mission.state` carries `artifact`. A stopped mission takes nothing new. `list(status)` is
offered everywhere (`offered_in` gives the rest only in the mission's own conversation).

**Plans** (`plans/`, core): one plan per conversation, table `plans`: `text`, `steps` [{title,
state todo|doing|done, checkpoint none|ask, model}], `waiting`. The rules are
`plans/store.py`'s only: at most one step doing; done starts the next open step, except done
on an `ask` step, which sets `waiting` and starts nothing. `set(steps, text)` writes it
whole, `step(n, state)` moves one. Every change appends `plan.state` {plan} to the
conversation and, for a mission, `mission.state`. A step with a model switches the
conversation to it when it becomes doing (`flow.follow_model`). The user speaking there
clears `waiting` (`answered_sync`). The agent's request carries the plan in its tail every turn
(`flow.block`). The runner's continue rule (`turns/runner.py`): a step whose calls are all
`needs_answer=False` (notebook and mind writes, `skills.ToolSpec`) with answer text ends the turn only when no step is open or
it waits; a `plan__step` that reached an ask checkpoint ends the turn at once.

`routines` (`schedules/skill.py`): `list`, `read(name)`, `write(name, label, prompt,
time|cron, on, note, after, wall_minutes)` and `run(name)`. A write goes through
`schedules/edit.save`, the same path and checks as PUT /schedules/{name}; a run it starts
has `runs.started_by` `her`.

Where it works (`conversations/here.py`): its system prompt says the conversation it is
in (chat, mission, a Routine's run, sub-agent or background task) with its intercom address;
the tail of a turn names the other conversations with a live turn.

`conversations`: `search(query, limit)`, FTS5 over user and assistant messages on every
conversation's active path, the current conversation left out; `read(conversation, before)`,
one conversation's active path from the end.

`web` (`skills/web.py`, guide `guides/web.md`, `[web]` in skills.toml): `search(query,
time_range, language, site, limit, page)` asks a self-hosted SearXNG (the address in
Settings, Features, seeded from `CELINE_SEARXNG_URL`) and returns
compact `[web:n]` blocks. `read(url, part)` takes a
URL or `web:n`, fetches it (httpx, 20 s, 5 MB HTML / 25 MB PDF, redirects followed by hand),
extracts the main text (trafilatura, pypdf) and returns one part of about `read_tokens`.
Only http(s) to global addresses is fetched: every hop's host is resolved and each address
must be global (`ipaddress.is_global`), and the peer address is checked again before the
body is read. Citations: it writes `[[web:n]]`; the runner's citation check
(`web.normalize`, `web.problems`, `web.link`) flags every n no web result in its context
showed (marked `|invalid`, as archive refs are) and stores the good ones as Markdown links.
Queries and fetched URLs leave the machine.

`email` (`skills/mail.py`, guide `guides/email.md`, always loaded): one email account over
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
Topic Search role in Settings, and whether anything embeds is that role alone
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
  dedupe, channels, cursors), `rest.py` (GET only), `gateway.py` (live messages, edits,
  deletes, catch-up after a drop), `sync.py`, `login.py`, `tools.py` (MCP read tools),
  `app.py`. It never writes to Discord.
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
  answer; it outlives the app. A connector's tools that are not read-only are hidden from
  Celine; `Supervisor.call` reaches them for the bridge. Tool results' records are
  archived and rendered as Messages archive lines, so they are citable.
- `bridge/`: a connector bridges when its `/admin/info` names a `write_chat` (wa-link:
  "Message yourself"; `part_chars` the longest message). Prompts there become intercom
  messages to "Bridge (WhatsApp)", read from the archive after a cursor (kv
  `bridge_cursor`) once the lease is held (60 s, renewed every 20 s). The agent's answer (the
  turn's last reply without a tool call, `conversations/tree.final_words`) goes back from
  the dispatcher's turn-end hook as it wrote it, only the markdown WhatsApp would show
  as raw symbols in WhatsApp's own syntax (`bridge/render.py`), every part in order.
  `say` sends its `intercom__send(to="user", urgent)`.
- `api/connectors.py`: the source pages' routes.

Write scope, enforced in the connectors and proven by their tests: discord-link's REST
client refuses any method but GET and nothing else writes to Discord; wa-link writes only
into the account's own "Message yourself" chat.

### Semantic

`kb/semantic.py`, built on its own every `smart_search_refresh_minutes` (Settings, General;
0 = off) and after a settings change. Each chat is cut into sessions at pauses, sessions into overlapping passages, and
every passage without a vector is embedded (`kb/sessions.py`, `kb/passages.py`,
`kb/embed.py`). `kv.passages_vec` records the width and the model's fingerprint; another
fingerprint resets the index. Vectors are the first `dims` dimensions as int8 plus sign
bits: a Hamming scan picks candidates, int8 cosine rescores them. Until a knowledge base's
first build has finished (`kv.passages_ready`), retrieval there is words only. Celine
searches through `kb/retrieve` (word search and semantic topic search, RRF, the reranker,
match windows).

### kind = documents

`kb/documents.py` (+ `documents_extract.py`, `documents_describe.py`). Your drawer, shared
with Celine, tied to no chat. The files under `path` are the content, kept byte for byte,
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
order under one GPU lease. Celine starts one with `image_generator__generate` or
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

**The agent's portrait** (`her.set_portrait`): one image, a blob its row names, png, jpg or
webp, the kind read from its bytes. The user sets it (`PUT /api/agent/portrait`); it sets it itself with the `appearance` skill's
`appearance__set_portrait(image)`: an image by its short id under the same rule as an
edit's source (`attachments.her_image`: one it made, or one the user attached to this
conversation), or a Documents image by its id, path or title. Either way
`her.changed` goes out; `GET /api/agent/portrait` serves it.

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
`intercom__start_chat(title, text)`. That tool, offered only in kinds background and
mission, first looks for what you already have from the same source in the last 24 h (a
bug report still open, or a thread to you); when there is one it lists each and it
decides whether its own is another matter (`different=true`). Else it creates a chat whose
first message is its own, keeps in kv (`chat_opening:<id>`) where it came from, and sends
the text to `user` from that chat. The title is 2 to 4 words.

## Schedules

`schedules/`. A schedule is a job as data: `var/settings/schedules/<name>.toml`, copied
once from `config/schedules/` with the partner prompts beside them (`*.md`), read only
from there. Fields: `label`, `cron` (the app's time zone), `prompt`,
`after` (the schedules whose runs of the same night it follows), `partner` (a prompt file
it talks with first) and `rounds`, `note` (a notebook path the answer is written to),
`role` (chat, tasks or small, default chat), `on`.

**Variables.** The prompt, the partner prompt and the note are filled from the run's
night, never from the clock (`schedules.fill`): `{{date}}`, `{{day_before}}`,
`{{weekday}}`, `{{result:<schedule>}}` (that schedule's answer of the same night), its
name, its pronouns and `{{username}}`.

**Runs** (table `runs`): one schedule at one slot, its planned time (UTC); `night` is the
night of the slot (`night_of`: before 12:00 in the app's zone belongs to that date's
night, from 12:00 on to the next date's); `state` waiting, running, done, failed, skipped
or stopped; `reason` why it waits; `conversation_id` its one conversation; `result`,
`error`; `started_at`, `finished_at`; unique (schedule, slot). Every change is `run.state
{run}` on the global feed.

**When a run exists.** At the next slot, on every `models.state` change, at the end of
every turn and after every change to the schedules, the scheduler (`schedules/runs.py`,
one per app) makes the run of every slot of a schedule that is on and came due since its
last look (kv `schedules.checked`). Slots missed while the app was off are made too: those
of the last 7 days wait like any other, older ones are written `skipped`.

**When a run starts.** A run waits until all of these hold; its `reason` says the first
that does not: every run it follows in the same night is finished; the model of its role
can answer now (`Router.can_answer`, "Waiting for <model>"); none of your chats has a live
turn; no other run is running. The queue is ordered by night, then by how deep the
schedule follows, then by slot.

**A run** is one background conversation (`trigger_key` `run:<run id>`) on the
schedule's role, its turns bound behind every interactive request and on the role's own
model only (`runs.binding`, the dispatcher reads it), with the filled prompt as its first
message from `system:schedules` and a rule: you are not here, its final answer is the result, `intercom__start_chat` for what
needs you. With a `partner`, it answers the prompt first, then the partner (on the run's
role, outside the turn loop, with read-only tools) and it alternate up to `rounds` lines;
its lines land as intercom messages from `system:<partner>`. Then it is asked for its
final answer. The run is over when its conversation and its sub-agents are quiet and
every sub-agent has reported. The result is written to `note` with `notebook__write`: a
note that was there gets the answer appended under "## <date> <label>"; a note it wrote
itself is the result and the app writes nothing over it.

**One try.** A run is never re-asked, rewritten or retried. When its turn fails and the
Router says the role's model cannot answer (unreachable, or none set), the run is `waiting`
for it ("Waiting for <model>"), its conversation kept; when the Router sees the model
answer, the failed turn is continued in that conversation (`turns.continue_turn`, the
stored context). Any other failure fails the run with the error, once: a model error, a
tool error that ends its turn, its turn stopped, or its ceiling (3 hours, or the schedule's
`wall_minutes`, counted from the run's start; a run that waited for its model starts
with a new clock). A run that was running when the app stopped is still running at the next start:
the dispatcher's sweep resumes its cut turn and the scheduler takes its answer once the
conversation is quiet (a partner's talk that was cut ends there). Stop (`POST
/api/runs/{id}/stop`) ends a waiting or running run as stopped; deleting a schedule skips
its runs.

**Routes.** `GET /api/schedules`; `PUT /api/schedules/{name}` (the whole schedule; the
prompts go into their history as `texts` `routine:<name>:prompt`); `DELETE
/api/schedules/{name}` (refused while another follows it); `POST /api/schedules/{name}/run`;
`POST /api/schedules/preview {night, text}`; `GET /api/runs?night=&schedule=&until=`; `GET
/api/runs/{id}`; `POST /api/runs/{id}/stop`. `GET
/api/background-conversations` gives a run's conversation its `run` {id, schedule, label, night}. The web
place is Routines (`views/Routines.tsx`).

## Intercom

`intercom/`. Durable, addressed, threaded text messages between you, its conversations
(agents) and the app, like mail. Every path by which something lands in a conversation
later is one intercom message: your message, a finished or stopped job, a reminder, a
sub-agent's brief and report, a trigger, a restart notice, a mission's brief. Rows are
never deleted.

Tables: `intercom` (one row per message: `from_addr`, `to_addr`, `kind`, `subject`, `text`,
`data` JSON for code, `wake`, `due_at`, `until_at`, `reply_to`, `thread`, `cancelled_at`),
`intercom_deliveries` (one row per recipient: `to_addr`, `conversation_id`, `state` pending,
delivered, read or cancelled, the `message_id` and `turn_id` it landed as) and
`intercom_tokens`. Kinds: `message`, `brief`, `job`, `report`, `trigger`, `notice`.

**Addresses** (`addresses.py`): stored `user`, `agent:<conversation id>`, `team:<conversation
id>` (that conversation and its sub-agents), `all` (every conversation with a live turn or
a running job, and every active mission), `system:guard` (Bastet) and `system:<name>` (a
sender only: `system:app`, `system:<skill>`, a named token). The agent's words `me`, `parent`,
`team`, `all`, `user`, `bastet`, a name, an id, `run:<id>` or `routine:<name>` are resolved
inside the send, the sender left out.

**Threads.** A message starts a thread (`thread` = its id) unless it has `reply_to`; then it
joins that message's thread. A reply without `to` goes to the sender of the message it
answers. Lists of threads (`store.threads`, `store.grouped`; `intercom__read` and `GET
/intercom/threads`) group them by counterpart; your plain chat messages (`store.PLAIN`) are
never threads.

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
step is done, the mission moves to Needs you. `missions__wait(why, in | at)` books its wake.
Check-ins (`watch.run`): an active mission with `check_in` minutes sends a `report` to its
report conversation, else to you, every `check_in` minutes; it costs its no turn.

## Events

One list of everything it did and everything that ran (`server/celine/events.py`), built
from the rows that exist, nothing stored twice. Each source is one SELECT with the same
first columns and a view that gives the row its words: `turn`, `tool`, `notebook`, `job`,
`message`, `run`, `guard`, `freeze`, `pass`, `work` and `bugreport`.

`GET /api/events` merges them newest first
by `(at, id)`; a page's `next` is that pair. `links.open` is the app path where it
happened (`intercom/store.py source_of` for a conversation, a note, a run, a thread).

`events.added {kind, conversation}` on the global feed says one landed: `Store.event` sends
it in the same write for the conversation events `core/bus.py lands` names; the other
sources call it themselves.

## Bug reports

`server/celine/bugreports/`, off with `[bugreport] enabled = false` in `config/skills.toml`.
It files a problem with the app with `bugreport__file(title, severity, what, where,
wish)` (`skill.py`, guide `guides/bugreport.md`). The rows are `bugreports` (`store.py`),
open until you close one. Each report is an Intercom thread: filing it sends a message to
you from the conversation that filed it (subject `Bug #<id>: <title>`), its id in
`bugreports.thread`, and your reply wakes it in that conversation. `GET
/api/bugreports?thread=` and `?state=open|closed` list them; `POST
/api/bugreports/{id}/close` closes one.

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
failures in a row the skill is `dead`; `POST /api/skills/{name}/restart` resets the count.
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
| `GET /status` | `owner` (it, as every page draws it: `{name, pronouns, color, portrait, named}`), `user`, `problems` (config problems), `hosts` (each provider up or down since, from the ping) |
| `GET /agent` | its package: `name, pronouns, color, portrait, named, persona` (as shown, names filled), `talk` (null: the shipped one), `talk_shipped`, `voice {banned, bias, on}`, `version` |
| `PUT /agent` | `If-Match: <version>`; any of `name, pronouns, color, persona, talk` (null: back to the shipped one), `voice`, checked whole before anything is written; global event `her.changed` |
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
| `PUT\|DELETE /conversations/{id}/messages/{mid}/voice-sample` | "Love this": its reply as a voice sample in its note `samples.md` |
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
| `GET /messages?q=&limit=&place=&archived=&trashed=` | words in the conversations with Celine: `{hits, titles}`; each hit with its `message_id`, `snippet` and `marks` ([start, end) of each matched word in it), best first |
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
| `PATCH /terminals/{id}` | `{may_type}`: whether Celine may type into this terminal of the user's; 400 on one of its own |
| `POST /terminals/{id}/control` | `{action}` stop, pause, resume or close → the daemon's `/control`; logged as a command of the user's (the button's label, the short word, on its terminals also the sentence it reads next) |
| `DELETE /terminals?state=ended`, `DELETE /terminals/{id}`, `POST /terminals/{id}/restore` | every ended terminal, or one, into the trash (`trashed_at`), or one back; nothing is deleted: their page and the commands log still reach them; `{trashed}`, `{ok}`. `DELETE /terminals/{id}?for_good=1` removes a trashed one |
| `GET /terminal-commands?terminal=&from=&result=&q=&mission=&limit=&before=` | the commands log, newest first: its shell calls and the user's typed lines and controls as one list |
| `POST /terminals/{id}/messages` | `{text, conversation_id?, command_id?}`: the user's text into the terminal's last conversation, the given one, or a new one (`"new"`) |
| `GET /artifacts` | its pages and documents, newest change first (`artifacts/api.py`): `{artifacts: [{id, kind, conversation_id, created_at, trashed_at, n, title, note, updated_at}]}` |
| `GET /artifacts/{id}` | one of its pages |
| `GET /artifacts/{id}/versions/{n}` | the page itself, with its own headers (`artifacts/page.py`): CSP `sandbox allow-scripts allow-forms` and the library hosts |
| `GET /artifacts/{id}/versions/{n}/source` | what it wrote, as `text/plain` under the page's headers: the Download, and the text the app renders a document from |
| `POST /artifacts/{id}/versions/{n}/restore` | that version copied to the top as a new one with the note `restored from v<n>`; the artifact as `GET` returns it; publishes `artifact.updated` |
| `DELETE /artifacts/{id}`, `POST /artifacts/{id}/restore` | into the trash, or back out of it (`trashed_at`); a trashed one still opens by its id; the artifact; publishes `artifact.updated` |
| `GET /shares?conversation=` | its shares that run, newest first (`shares.py`): `{shares: [{token, title, port, path, conversation_id, public, created_at, ends_at, state, url}]}`, `state` live or down from a probe of its box, `ends_at` when it ends (or ended), `url` its open route |
| `GET /shares/{token}` | one share, `state` live, down or ended |
| `GET /shares/{token}/open` | sets the share's cookie and redirects (303) to `/share/<token>/`; a share that does not run is a 404 page |
| `POST /shares/{token}/stop` | ends the share for everyone; the share, `state` ended |
| `GET /browser` | its browser box's status (`browser/api.py`): `{state, reason, started, taken, screen, tabs}`, `state` starting, up or down from the box, `unreachable` when it does not answer |
| `POST /browser/control` | `{action}` take, give or restart → the box's `/control`: take and give answer `{taken}`, restart its status once it is up or down again (30 s at most) |
| `GET /browser/calls?q=&failed=&before=&limit=` | its browser calls, newest first (the Browser place's History): `{calls, total, failed, next}` |
| `GET /events?kind=&day=&state=&decision=&rule=&mode=&before=&limit=` | Events, everything it did and everything that ran, newest first (`events.py`, Events above): `{events, total, failed, undecided, next, kinds, days, guard}` |
| `POST /missions` | `{name?, brief, report_to?, time_limit?, check_in?}` (`check_in`: `30 min` or `2 h`, the check-ins' cadence) (any other key is a 400; `missions/api.py`) |
| `GET /missions?status=` | every mission, newest first, with its conversation's title (`missions/store.list`), and `counts` per state, archived and trashed (`missions/store.counts`) |
| `GET /sub-agents?conversation=` | every sub-agent (of one conversation, or all), newest first: `id, parent, name, access, folder, model, cli, working` (not `report.quiet`), `step` (the tool at work), `report` (the latest) |
| `POST /conversations/{cid}/sub-agents` | `{brief, name, access?, folder?, model?, cli?}` (any other key is a 400): a sub-agent of the conversation, exactly as `sub_agents__start` makes one (`skill.begin`); `{conversation_id}` |
| `POST /sub-agents/{id}/stop` | its turn, its jobs and what it scheduled, as `sub_agents__stop` (`skill.stop_agent`); `{stopped}` |
| `PATCH /missions/{cid}` | `{name?, brief?, report_to?, time_limit?, check_in?, steps?}`, the given fields only (any other key is a 400); the time limit's notice cancelled and scheduled again or removed |
| `POST /intercom` | `{to, text, subject?, reply_to?, in?, at?, until?}` (`intercom/api.py`): from `user` with the app session, from `system:<name>` with a named token |
| `GET /intercom?to=&from=&thread=&scheduled=&after=&limit=` | messages to and from stored addresses, in a thread, above id `after`, oldest first, each with its `state` (pending, sent, cancelled) and `unread`; `scheduled=true` |
| `GET /intercom/threads?with=&kind=&unread=&limit=&cursor=` | threads grouped by who they are with, the newest activity first (`ThreadsPage`): `{groups: [{name, addresses, threads, waiting, items, more}], jobs: {count, newest}, next}` |
| `POST /intercom/read` | `{ids}`: the user's deliveries of these become `read`; `?all=1`: everything that waits for the user (Dismiss all); `{read}` |
| `POST /intercom/threads/{id}/conversations` | Continue in a chat: a new chat with it (titled by the thread's subject) that carries the thread, its messages landed as one `notice` from `system:app` without waking it |
| `GET /intercom/stream?to=&after=` | server-sent events, `id:` the message id: every message above `after` (or `Last-Event-ID`), then each new one as it is sent (woken by the send, not by the bus); a comment line every 15 s |
| `POST /intercom/{id}/stop` | cancels a scheduled message that has not landed; `{ok}`; 404 `This message already landed or was cancelled.` |
| `GET /tokens`, `POST /tokens`, `POST /tokens/{id}/revoke` | named tokens (Settings, Security): the list without hashes; `{name}` makes one, `{name, token}` shows it once; revoke hides it, the row stays |
| `POST /missions/{cid}/stop` | status `stopped`, the time limit's notice cancelled, the conversation aborted (`abort_conversation`); `{ok}` |

**Slash commands** (`api/commands.py`, a small registry: `@command(name, description, args=,
aliases=, when=)` adds one): `/compact [focus]` (summarise now), `/think [level]` and its
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
`tool.started`, `tool.progress`, `tool.done`, `tool.gated`, `tool.detached`, `tool.reset`,
`job.state`, `guard.banner`, `images.batch`, `music.batch`, `version.changed`,
`overrides.changed`, `llm.queue`, `context.*` (see Context), `conversation.settings`,
`citations.checked`, `terminal.state`, `error`. `turn.state` carries the conversation's live
`queue`, `message.created` the message's `meta`.

The global feed carries what every open page needs across conversations: turn and job
states, titles, the archive's tasks and sources, the model queue, settings changes, and
bare signals such as `terminal.state` and `browser.state`; `conversation.created`,
`conversation.deleted`, `conversation.restored`, `conversation.pinned`,
`conversation.archived`, `conversation.unarchived`; `agent.state`, `mission.state`,
`intercom.state`, `artifact.updated`, `images.batch` and `music.batch`, `context.changed`
(what a conversation carries by need came or went), `events.added` (a row of Events
landed), `notice` (one item for a system notification while the tab is hidden: its
reply in a chat, an Intercom message to you, a mission waiting for you, a failed run, a
request to approve; `{kind, title, body, link, tag}`, events.py `notice`, web
lib/notify.ts), `mood`, `her.changed`, `her.hold`, `frozen.changed`, `guard.passes`,
`models.state`, `llm.queue` and `kb.*`. The agent's shell and browser calls' `tool.started`,
`tool.done` and `tool.detached` pass through it as ids.

`GET /api/changes?since=<global id>&conversations=<id:seq,...>` is one call for a client
coming back: `{global, resync, conversations, gone, jobs, unread, agents}`: the newest
global id, whether `since` is below what the feed keeps, the given conversations whose seq
moved, the ids that no longer exist, the jobs that finished since, how many intercom
messages wait for you, and every sub-agent as `GET /sub-agents` gives it.

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
are in `styles/tokens.css`. The app installs as a PWA (`web/public/manifest.webmanifest`);
there is no service worker.

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

Celine's section: the conversation list (pinned, then by date, paged with the list's
cursor; each row's preview is the row's `last_line`), the transcript
(`components/Transcript.tsx` on the kit's Message, with the message actions of
`components/MessageActions.tsx`), the composer (`components/Composer.tsx`: draft per
conversation, attachments, slash commands, Impersonate) and the side panel (Model, Tools,
Pages, Context). `useConversation` keeps the snapshot's `queue` up to date from `turn.state`
so the page never reads idle while a queued turn waits; `tool.started` lands on its
`message_id`. The side panel's Background jobs list reads `GET /jobs?conversation=`,
reloaded on that conversation's `job.state` events. The notebook (`components/Notebook.tsx`,
its own place at `/notebook`) edits its files through `/api/notebook/*`; paths are confined
to its `notebook/`, protected files are read-only.

## Operations

`celine doctor` loads the config and prints one line per check (problems, database, providers,
knowledge bases, skills, MCP servers, triggers). Exit 1 if anything is marked WARN.

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
