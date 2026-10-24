# Configuration

Where to change who it is, what it does and what is switched on. Most of it is also
in the app (the agent's page, Routines, Settings).

## Personality

The agent's shipped persona is `config/her/persona.md` (who it is and how it works) and
`config/her/talk.md` (how it sounds, by example lines). A fresh install copies both into
its row; from then on you edit them in the onboarding or on the agent's page, and the files
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

`config/her/voice.toml` has `banned` (words its model is pushed away from, at `bias`),
`phrases` (longer things it says that are not its own) and `patterns` (regexes that are
only counted). It is copied into its row once; edit it there (The agent's page, Banned words).

## Advanced

The time limits and sizes (turns at once, tool, job and turn time limits, messages per
request, what is kept, attachment sizes) are Settings, Advanced, saved in
`var/settings/general.toml` and applied at once. `config/celine.toml` holds only how the
app is served (host, port, login, the data folder) and is read at start.

## Routines

A routine is `config/schedules/<name>.toml`: lowercase letters, digits, `-` or `_`,
starting with a letter. Copy `config/schedules/_template.toml.example` (never loaded)
and rename it. Or add one on the Routines page.

| Key | What |
|---|---|
| `label` | the name shown, 1 to 60 characters (required) |
| `summary` | one sentence under its name in the onboarding's Routines card, at most 120 characters |
| `cron` | minute hour day month weekday, in your time zone (required) |
| `prompt` | what it is asked (required) |
| `after` | names of routines it follows on the same night |
| `partner` | a `.md` prompt beside the file it talks with first; `""` for none |
| `rounds` | the partner's lines at most, 1 to 30 (default 8) |
| `note` | notebook path (`.md`) its answer is written to; `""` for none |
| `role` | the model role the run uses (default `chat`) |
| `on` | `false`: it never starts by itself |
| `idle_only` | `true`: runs only if nobody used the app for 30 minutes |
| `wall_minutes` | most minutes a run may take, 1 to 180 |
| `daytime` | `true`: `{{date}}` is the calendar date of the slot |

The prompt, partner file and note can use `{{date}}`, `{{day_before}}`, `{{weekday}}`,
`{{username}}`, `{{name}}`, the pronouns, `{{result:<routine>}}` (another routine's
answer from the same night) and `{{mood}}`, `{{on_mind}}`, `{{tool_failures}}`,
`{{voice_counts}}`. Nightly backups are kept for Backups kept in Settings, Advanced
(default 7 days).

## Texts the model reads

What a model reads lives where the one who edits it works. The agent's persona, talk and voice
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

Settings, Features (or `var/settings/features.toml`, `shellbox = false`). A feature not
named is on, but Shares and Guard, which are on only once saved on (`shares = true`,
`guard = true`); switching Shares on asks first, with what a share's link gives whoever holds it. The onboarding shows a card
for every feature whose service answers, and always for Shares, Discord, WhatsApp (their
card says how to run the connector), Guard and Routines (switch each routine on or off and set its time). Off means its skills are not built, and for the connectors and services
they are stopped at once: a switch, or a changed address in Settings, Features, is live
without a restart. Docker starts a service only if its profile is in `COMPOSE_PROFILES`
in `.env`.

| id | Label | About | Needs | Compose profile |
|---|---|---|---|---|
| `shellbox` | Shellbox | An isolated Linux computer with a terminal, files and programs of its own, nothing on your machine. | the shellbox service | `shellbox` |
| `shares` | Shares | It shows you a page, game, tool or file from its box in a new tab. | the shellbox feature on | |
| `browser` | Browser | An isolated Brave browser with ad blocking: pages, logins and forms, with a history you can review and a live view you can watch. | the browser service | `browser` |
| `images` | Images | Pictures made with ComfyUI in the styles of the presets you keep. | a ComfyUI provider | |
| `music` | Music | Music made with ComfyUI from a few words about the track. | a ComfyUI provider | |
| `web` | Web search | Web search through your own SearXNG, with the pages found read as text or in the browser. | SearXNG | `web` |
| `discord` | Discord | Your Discord history in the archive, and a chat from Discord itself. | the discord-link service | `discord` |
| `whatsapp` | WhatsApp | Your WhatsApp history in the archive, and a chat from WhatsApp itself. | the wa-link service | `whatsapp` |
| `email` | Email | A mailbox of yours read on your behalf: what came in, searchable and summarised. | an IMAP account in Settings, Email | |
| `vault` | Vault | A Vaultwarden collection with the logins used in the browser on your behalf. | a Vaultwarden in Settings, Vault | |
| `guard` | Guard | Bastet, a second model that checks risky actions before they run and asks you when unsure. | a decision model (the Titles and descriptions role) | |
| `documents` | Documents | A folder of your documents, searched, read and cited. | nothing | |
| `messages` | Messages | Your chat history in one archive, searched by word or by topic. | Discord or WhatsApp | |
| `artifacts` | Artifacts | Built pages: a chart, a table, a small app, kept with every version. | nothing | |
| `routines` | Routines | Jobs on a schedule, a morning digest or a nightly tidy of the notes; change a time or switch one off. | nothing | |
| `missions` | Missions | A long job carried over days, with check-ins as it goes. | nothing | |
| `sub_agents` | Sub-agents | Parts of a big job handed to helpers, their results collected. | nothing | |

`COMPOSE_PROFILES=shellbox,browser,discord,whatsapp,web` starts all of them; add
`reranker` for the reranker.

## Skills and MCP

`config/skills.toml` has one section per built-in skill, such as `[shell]`, with
`enabled = true` or `false` as the global default (a conversation can override it).

`[code]` is its coding agent: a coding agent server in its shellbox (opencode by default;
the box picks the backend with the `CODING_AGENT` environment variable, one module in
`shellbox/agents/`) that works on a folder of its own in the background with a model it
picks from the ones you allow it. `poll_seconds` is how long it waits between two
looks at a session, `result_chars` how much of its answer it reads. It is part of the
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
`${VAR:-default}` read the environment. A server that fails to start is reported and does
not stop it.

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
