# Configuration

Where to change who it is, what it does and what is switched on. Most of it is also
in the app (the agent's page, Routines, Settings).

## Personality

The agent's shipped persona is `config/her/persona.md` (who it is and how it works) and
`config/her/talk.md` (how it sounds, by example lines). A new install copies both into
its database; from then on you edit them in the onboarding or on the agent's page, and the files
are only the seed for a new install. `config/her/owner.md` (its notebook rules) is
appended to every persona.

Placeholders, filled when a request is built and never stored filled. What you type is
stored exactly as typed: a name written out stays that name, a placeholder you write
follows a rename.

| Placeholder | Becomes |
|---|---|
| `{{username}}`, `{{user}}` | your name (Settings, General) |
| `{{name}}`, `{{char}}` | its name |
| `{{she}}`, `{{her}}`, `{{herself}}`, `{{them}}` | its pronouns: she/her/herself/her, he/his/himself/him or they/their/themselves/them |

A capital first letter (`{{She}}`) gives a capital word.

## Writing style

`talk.md` shows how it sounds by example: your line, then its own, one beat each. Keep that
shape when you add lines.

`config/her/voice.toml` has `banned` (Banned words: words the model avoids, as strongly as
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
reranking), Notebook (the index in every prompt), Past conversations, Code, Sub-agents.
Each skill declares them beside its code, so a skill you remove takes its section with
it. `general.toml` keeps them in a table per skill:

```toml
[web]
results = 10
pdf_max_mb = 50
```

## Routines

A routine is `config/schedules/<name>.toml`: lowercase letters, digits, `-` or `_`,
starting with a letter. Copy `config/schedules/_template.toml.example` (never loaded)
and rename it. Or add one on the Routines page.

| Key | What |
|---|---|
| `label` | the name shown, 1 to 60 characters (required) |
| `summary` | one sentence under its name in the onboarding's Routines step, at most 120 characters |
| `cron` | minute hour day month weekday, in your time zone (required) |
| `prompt` | what it is asked (required) |
| `after` | names of routines it follows on the same night |
| `partner` | the Reviewer prompt: a `.md` file in the same folder; the routine talks with the reviewer first. `""` for none |
| `rounds` | the most Turns the reviewer gets, 1 or more (default 8) |
| `note` | notebook path (`.md`) its answer is written to; `""` for none |
| `role` | the model role the run uses (default `chat`) |
| `on` | `false`: it runs only when you start it with Run now |
| `idle_only` | `true`: runs only if nobody used the app for 30 minutes |
| `wall_minutes` | most minutes a run may take, 1 to 180 |
| `daytime` | `true`: `{{date}}` is the date the routine runs on (otherwise it is the date of its night, the day before for a run after midnight) |

The prompt, partner file and note can use `{{date}}`, `{{day_before}}`, `{{weekday}}`,
`{{username}}`, `{{name}}`, the pronouns, `{{result:<routine>}}` (another routine's
answer from the same night) and `{{mood}}`, `{{on_mind}}`, `{{tool_failures}}`,
`{{voice_counts}}`. How many days of nightly backups are kept is set in Settings,
Advanced, Backups kept (default 7).

## Texts the model reads

You edit each text a model reads in the place where it is used. The agent's persona, Example lines and Banned words
are on the agent's page; a routine's prompt is on the Routines page; its seeded notes
(`config/notebook_seed/`, copied once) are in her notebook from then on; Bastet's persona,
its replies to Celine and its notes to you are on Settings, Guard (each with History; the
shipped `config/guard/bastet.md`, `talk.md` and `note.md` are their defaults, Use the default
goes back to them), and its judge prompts are `config/guard/`. Everything else a model reads
(a skill guide, the title or summary prompt) is code and ships with the package,
`server/celine/**/*.md`.

With Docker the image carries `config/`; to run your own copy, mount it over the image's
in `docker-compose.yml` under the `celine` service's `volumes`:
`- ./config/guard:/app/config/guard:ro`.

## Features

Settings, Features (or `var/settings/features.toml`, `shellbox = false`). A feature that is
not listed there is on, except Shares, Guard, Dictation and Read aloud, which stay off until
you turn them on (`shares = true`, `guard = true`, `speech_input = true`,
`speech_output = true`); switching Shares on asks first, with what a share's link gives whoever holds it. The onboarding's Features
step lists every feature by tier (Core, on with no switch; Base modules, what ships in the repo;
User modules, a plugin of `<data>/plugins` with no place of its own) and then gives a setup step
to each feature switched on there that has a form or a connection (the Shellbox and Her Mac
share one step, Her computer; the Discord and WhatsApp steps say how to start the connector;
the Routines step turns each routine on or off and sets its time). Off means its skills are not built, and for the connectors and services
they are stopped at once: a switch, or a changed address in Settings, Features, is live
without a restart. Docker starts a service only if its profile is in `COMPOSE_PROFILES`
in `.env`.

| id | Label | About | Needs | Compose profile |
|---|---|---|---|---|
| `shellbox` | Shellbox | A Linux computer of its own in a container, with a terminal, files and programs. It does not touch your machine. | the shellbox service | `shellbox` |
| `shares` | Shares | Shows you a page, game, tool or file from the shellbox in a new tab. | the shellbox feature on | |
| `browser` | Browser | An isolated Brave browser with ad blocking: pages, logins and forms, with a history you can review and a live view you can watch. | the browser service | `browser` |
| `images` | Images | Pictures made with ComfyUI, in the styles of your presets. | a ComfyUI provider | |
| `music` | Music | Music made with ComfyUI from a few words about the track. | a ComfyUI provider | |
| `speech_input` | Dictation | Speak a message, read it over, then send it. | a Dictation model | |
| `speech_output` | Read aloud | Replies read out loud, in a voice you pick. | a Read aloud model | |
| `web` | Web search | Web search through your own SearXNG, with the pages found read as text or in the browser. | SearXNG | `web` |
| `discord` | Discord | Your Discord history in Messages, and it can read your Discord live. | the discord-link service | `discord` |
| `whatsapp` | WhatsApp | Your WhatsApp history in Messages, and it can read your WhatsApp live. | the wa-link service | `whatsapp` |
| `email` | Email | It reads your mailbox for you: new mail, search and summaries. | an IMAP account in Settings, Email | |
| `vault` | Vault | Logins from your Vaultwarden, which it types into websites in the browser for you. | a Vaultwarden in Settings, Vault | |
| `guard` | Guard | Bastet, a second model that checks risky actions before they run and asks you when unsure. | a decision model (the Titles and descriptions role) | |
| `documents` | Documents | A folder of your documents, searched, read and cited. | nothing | |
| `messages` | Messages | Your chat history in one archive, searched by word or by topic. | Discord or WhatsApp | |
| `artifacts` | Pages | Pages it builds for you, like a chart, a table or a small app, with every version kept. | nothing | |
| `routines` | Routines | Jobs that run on a schedule, like a morning summary or a nightly tidy of its notes. | nothing | |
| `missions` | Missions | Long jobs it works on over days, with updates as it goes. | nothing | |
| `sub_agents` | Sub-agents | It hands parts of a big job to helpers and collects their results. | nothing | |

`COMPOSE_PROFILES=shellbox,browser,discord,whatsapp,web` starts every optional service these features need; add
`reranker` for the reranker.

## Skills and MCP

`config/skills.toml` has one section per built-in skill, such as `[shell]`, with
`enabled = true` or `false` as the global default (a conversation can override it), and
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
give it words: one sentence per tool, and one for several calls of the same tool in a row
(`{n}` is how many):

```toml
[servers.filesystem.words]
read_text_file = "Read {path|a file}"       # {argument|what it says without one}
search_files   = "Searched {path} for \"{pattern}\""

[servers.filesystem.groups]
read_text_file = "Read {n} files"           # calls in a row with the same words: one row
```

A plugin's `[servers.<id>]` takes the same tables (`plugins/mac/plugin.toml`).

## Image and music presets

`config/image_presets/<id>.toml` plus `<id>.json`, and the same in `config/music_presets/`.
The JSON is the ComfyUI workflow exported with Export (API); its values are the
defaults. The TOML has `label`, `family`, `good_at`, `prompting`, `workflow` (the JSON
file) and `[parameters.<name>]` entries that name the nodes and inputs the app fills; the
full schema is the docstring of `server/celine/comfyui/presets.py`. The model, LoRA and
VAE file names inside the workflow must exist on your ComfyUI server under the same
names, or the preset fails when it uses it. A preset with a problem is left out and
named in the configuration banner.

Settings, Images & Music edits every preset (an edit is saved in
`var/settings/image_presets/` or `music_presets/`; Reset to defaults goes back) and takes
your own workflow: export it with Export (API), put a placeholder as the whole value of
each input the app fills, and upload it. The placeholders are `{{prompt}}`, `{{seed}}`,
`{{width:1024}}`, `{{height:1024}}` (a new image), `{{image}}` to `{{image4}}` (an edit),
`{{negative}}`, `{{model:file}}`, `{{mode:value}}` for images and `{{style}}`, `{{seed}}`,
`{{lyrics}}`, `{{duration:30}}`, `{{instrumental:true}}` for music; after the colon is the
default. Download workflow gives a preset back in the same form.
