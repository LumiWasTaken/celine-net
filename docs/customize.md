# Configuration

Where to change who it is, what it does and what is switched on. Most of it is also
in the app (the agent's page, Routines, Settings).

## Personality

The agent's shipped persona is `config/agent/persona.md` (who it is and how it works) with
`config/agent/owner.md` (its notebook rules) after it; its Example lines are
`config/agent/talk.md` (how it sounds). A new install copies the persona and owner.md into
its database once; from then on you edit the persona in Setup or on the agent's page, and
the two files only seed a new install. The Example lines come from `talk.md` until you
save them in Setup or on the agent's page; from then on your text is used.

Placeholders, filled when a request is built and never stored filled. What you type is
stored exactly as typed: a name written out stays that name, a placeholder you write
follows a rename.

| Placeholder | Becomes |
|---|---|
| `{{username}}`, `{{user}}` | your name (Settings, General) |
| `{{name}}`, `{{char}}` | your agent's name |
| `{{pronouns}}` | your agent's pronouns: she/her, he/him, they/them or it/its |

A text that speaks of your agent names it with `{{name}}`; `{{pronouns}}` is for a prompt
that tells a model which pronouns your agent uses.

## Writing style

`talk.md` shows how it sounds by example: your line, then its own, one beat each. Keep that
shape when you add lines.

`config/agent/voice.toml` has `banned` (Banned words: words the model avoids, as strongly as
`bias` sets), `phrases` (longer phrases that do not sound like it) and `patterns` (regular
expressions the app only counts, for statistics). A new install copies it once; after that,
edit them on the agent's page under Banned words.

## Advanced

The time limits and sizes (turns at once, tool, job and turn time limits, messages per
request, what is kept, attachment sizes) are Settings, Advanced, saved in
`var/settings/general.toml` and applied at once. `config/celine.toml` holds only how the
app is served (host, port, login, the data folder) and is read at start.

Below the app's own, each skill's numbers have a section under the skill's name: Web
(search results, time limits, the largest page and PDF in MB), Messages (result lengths,
reranking), Notebook (the index in every prompt), Past chats, Code, Sub-agents.
Each skill declares them beside its code, so a skill you remove takes its section with
it. `general.toml` keeps them in a table per skill:

```toml
[web]
results = 10
pdf_max_mb = 50
```

## Routines

A routine is a schedule file, `<name>.toml`: lowercase letters, digits, `-` or `_`,
starting with a letter. The shipped ones in `config/schedules/` are copied once into
`var/settings/schedules/` (`app/settings/schedules/` with Docker) at the first start with
Routines on; from then on only that folder is read. To add one, copy
`config/schedules/_template.toml.example` (never loaded) into that folder and rename it,
or press New schedule on the Routines page.

| Key | What |
|---|---|
| `label` | the name shown, 1 to 60 characters (required) |
| `summary` | one sentence under its name in the Routines step of Setup, at most 120 characters |
| `cron` | minute hour day month weekday, in your time zone (required) |
| `prompt` | what it is asked (required) |
| `after` | names of routines it runs after on the same day (Runs after) |
| `reviewer` | the Reviewer prompt: a `.md` file in the same folder; the routine talks with the reviewer first. `""` for none |
| `turns` | the most Turns the reviewer gets, 1 or more (default 8) |
| `note` | notebook path (`.md`) its result is written to; `""` for none |
| `role` | the model role the run uses (default `chat`) |
| `on` | `false`: it runs only when you start it with Run now |
| `idle_only` | `true`: runs only if nobody used the app in the last 30 minutes, else that run is skipped |
| `wall_minutes` | most minutes a run may work, 1 or more; waiting for a model does not count. The Routine time limit in Settings, Advanced (default 180) caps it and applies when it is left out |
| `daytime` | `true`: `{{date}}` is the date the routine runs on (otherwise it is the day the run belongs to on the Routines page, the day before for a run after midnight) |
| `on_fail` | When a run fails: `"retry"` (default) runs a run that failed on an error again, up to three times the same day, 15 minutes, 1 hour and 4 hours after the one before, never past its next run; `"skip"` ends it Skipped with no notice |

The prompt, reviewer file and note can use `{{date}}`, `{{day_before}}`, `{{weekday}}`,
`{{username}}`, `{{name}}`, the pronouns, `{{result:<routine>}}` (another routine's
result from the same day) and `{{mood}}`, `{{on_mind}}`, `{{tool_failures}}`,
`{{voice_counts}}`. How many days of nightly backups are kept is set in Settings,
Advanced, Backups kept (default 7).

## Texts the model reads

You edit each text a model reads in the place where it is used. The agent's persona, Example lines and Banned words
are on the agent's page; a routine's prompt is on the Routines page; its seeded notes
(`config/notebook_seed/`, copied once) are in its notebook from then on; Bastet's persona,
its replies to your agent and its messages to you are on Settings, Guard (each with History; the
shipped `config/guard/bastet.md`, `talk.md` and `note.md` are their defaults, Use the default
goes back to them). Everything else a model reads (Bastet's check prompts in
`config/guard/`, a skill guide, the title or summary prompt, the lines of a routine run;
shipped as `server/celine/**/*.md`) is on Settings, Advanced, Prompts: an edit is stored in
`var/prompts` with its History, an app update leaves it alone, and Use the default goes
back to the shipped text.

With Docker the image carries `config/`; to run your own copy, mount it over the image's
in `docker-compose.yml` under the `celine` service's `volumes`:
`- ./config/guard:/app/config/guard:ro`.

## Features

Settings, Features (or `var/settings/features.toml`, `shellbox = false`). A feature that is
not listed there is on, except Shares, Guard, Dictation, Read aloud and the plugins (such as
`<name>'s Mac`), which stay off until you turn them on (`shares = true`, `guard = true`,
`speech_input = true`, `speech_output = true`, `mac = true`; a plugin whose `plugin.toml`
says `start_on = true` starts on); switching Shares on asks first, with what a share's link gives whoever holds it. The Features
step of Setup lists every feature by tier (Core, on with no switch; Built-in, what ships in the repo;
Plugins, your plugins, [plugins.md](plugins.md); one that owns a place is a base module) and then gives a setup step
to each feature switched on there that has a form or a connection (the Shellbox and `<name>'s Mac`
share one step, `<name>'s computer`; the Discord and WhatsApp steps say how to start the connector;
the Routines step turns each routine on or off and sets its time). Off means its skills are not built, and for the connectors and services
they are stopped at once: a switch, or a changed address in Settings, Features, is live
without a restart. Docker starts a service only if its profile is in `COMPOSE_PROFILES`
in `.env`.

| id | Label | About | Needs | Compose profile |
|---|---|---|---|---|
| `shellbox` | Shellbox | A Linux computer of its own in a container, with a terminal, files and programs, apart from your machine. | the shellbox service | `shellbox` |
| `shares` | Shares | Shows you a page, game, tool or file from the shellbox in a new tab. | the shellbox feature on | |
| `browser` | Browser | An isolated Brave browser with ad blocking: pages, logins and forms, with a history you can review and a live view you can watch. | the browser service | `browser` |
| `images` | Images | Images made with ComfyUI, in the styles of your presets. | a ComfyUI provider | |
| `music` | Music | Music made with ComfyUI from a few words about the track. | a ComfyUI provider | |
| `speech_input` | Dictation | Speak a message, read it over, then send it. | a Dictation model | |
| `speech_output` | Read aloud | Replies read out loud, in a voice you pick. | a Read aloud model | |
| `web` | Web | Web search through your own SearXNG, with the pages found read as text or in the browser. | SearXNG | `web` |
| `discord` | Discord | Your Discord history in Messages, and it can read your Discord live. | the discord-link service | `discord` |
| `whatsapp` | WhatsApp | Your WhatsApp history in Messages, and it can read your WhatsApp live. | the wa-link service | `whatsapp` |
| `email` | Email | It reads your inbox for you: new email, search and summaries. | an IMAP account in Settings, Email | |
| `vault` | Vault | Logins from your Vaultwarden, which it types into websites in the browser for you. | a Vaultwarden in Settings, Vault | |
| `guard` | Guard | Bastet, a second model that checks the agent's risky tool calls before they run. | a Decision model (its own role in Settings, Models) | |
| `documents` | Documents | A folder of your documents, searched, read and cited. | nothing | |
| `messages` | Messages | Your chat history in one archive, searched by word or by topic. | Discord or WhatsApp | |
| `artifacts` | Pages | Pages it builds for you, like a chart, a table or a small app, with every version kept. | nothing | |
| `routines` | Routines | Prompts that run on a schedule, like a morning summary or a nightly tidy of its notes. | nothing | |
| `missions` | Missions | Long tasks it works on over days, with updates as it goes. | nothing | |
| `sub_agents` | Sub-agents | It hands parts of a big task to sub-agents and collects their results. | nothing | |
| `mac` | `<name>'s Mac` | Its own Mac: its screen, a shell and its files, and a browser. A plugin (`plugins/mac/`). | the Mac set up with the command its form shows (`plugins/mac/README.md`) | |

`COMPOSE_PROFILES=shellbox,browser,discord,whatsapp,web` starts every optional service these features need; add
`reranker` for the reranker.

## Skills and MCP

`config/skills.toml` has one section per built-in skill, such as `[shell]`, with
`enabled = true` or `false` as the global default (a chat can override it), and
nothing else: a skill's numbers are on Settings, Advanced. A skill whose module (or
folder) is removed is skipped: no tools, no Advanced section, and its saved numbers are
dropped.

`[code]` is its coding agent: a coding agent server in its shellbox (opencode by default;
the box picks the backend with the `CODING_AGENT` environment variable, one module in
`shellbox/agents/`) that works on a folder of its own in the background with a model it
picks from the ones you allow it. Settings, Advanced, Code: how often it checks a running
session, and how much of the coding agent's answer it reads. It is part of the
Shellbox feature: off with it.
`config/mcp.toml` adds MCP servers; each becomes a skill named after its key:

```toml
[servers.filesystem]
enabled   = true
transport = "stdio"
sends     = "house"          # where its writing tools send: public, people, house
command   = "npx"
args      = ["-y", "@modelcontextprotocol/server-filesystem", "~/projects"]
```

`transport = "http"` takes `url` and `[servers.<id>.headers]` instead. `${VAR}` and
`${VAR:-default}` read the environment. A server that fails to start is shown as an error,
and the app keeps running.

In the chat, a call to one of its tools shows as "Used filesystem read file" unless you
give it words: one sentence per tool, and one for several calls of the same tool. The
calls between two of its messages are one row, with one part per tool in the order first
seen ("Read 3 files, searched notes"); a tool called more than once there says its group
words (`{n}` is how many):

```toml
[servers.filesystem.words]
read_text_file = "Read {path|a file}"       # {argument|what it says without one}
search_files   = "Searched {path} for \"{pattern}\""

[servers.filesystem.groups]
read_text_file = "Read {n} files"           # two or more calls in one row: one part
```

A plugin's `[servers.<id>]` takes the same tables, and [plugins.md](plugins.md) lists every
key, with `place` for showing a server's calls in Terminals or Browser.

## Image and music presets

`config/image_presets/<id>.toml` plus `<id>.json`, and the same in `config/music_presets/`.
The JSON is the ComfyUI workflow exported with Export (API); its values are the
defaults. The TOML has `label`, `family`, `good_at`, `prompting`, `workflow` (the JSON
file) and `[parameters.<name>]` entries that name the nodes and inputs the app fills; the
full schema is the docstring of `server/celine/comfyui/presets.py`. The model, LoRA and
VAE file names inside the workflow must exist on your ComfyUI server under the same
names, or the preset fails when it uses it. A preset with a problem is left out and
listed on Settings, General.

Settings, Images & Music edits every preset (an edit is saved in
`var/settings/image_presets/` or `music_presets/`; Reset to defaults goes back) and takes
your own workflow: export it with Export (API), put a placeholder as the whole value of
each input the app fills, and upload it. The placeholders are `{{prompt}}`, `{{seed}}`,
`{{width:1024}}`, `{{height:1024}}` (a new image), `{{image}}` to `{{image4}}` (an edit),
`{{negative}}`, `{{model:file}}`, `{{mode:value}}` for images and `{{style}}`, `{{seed}}`,
`{{lyrics}}`, `{{duration:30}}`, `{{instrumental:true}}` for music; after the colon is the
default. Download workflow gives a preset back in the same form.
