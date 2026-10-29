# Running celine-net as a service (Docker)

The stack is one compose project, meant for a service VM or any machine with Docker.
It needs no GPU: the models run on other machines and you add them in the app. The
reranker is optional in the stack (profile `reranker`, on the CPU or on this machine's
GPU). Every other service except `celine` is optional too: see Profiles.

```
 browser ──► [reverse proxy, https] ──► celine :8766 (the only published port)
                                          ├─► discord-link :8768   the Discord account
                                          ├─► wa-link      :8767   the WhatsApp linked device
                                          ├─► searxng      :8080   web search
                                          ├─► reranker     :8080   orders archive search results
                                          ├─► shellbox     :8770   Celine's own Linux computer
                                          ├─► browser      :8771   Celine's own Brave
                                          └─► model servers        added in Settings, Models
 volume "data":  app/   discord-link/   wa-link/   reranker/   shellbox/
```

| Service | Image | What it does |
|---|---|---|
| `celine` | `celine-app` | The server and the web UI. |
| `discord-link` | `celine-discord-link` | Owns the Discord login, keeps a journal, serves Celine's Discord read tools. |
| `wa-link` | `celine-wa-link` | Owns the WhatsApp linked device, keeps a journal, serves Celine's WhatsApp read tools. |
| `searxng` | `searxng/searxng` | A private metasearch engine for Celine's web skill. |
| `reranker` | `ggml-org/llama.cpp:server` | bge-reranker-v2-m3 on llama-server, for the Reranking task. Only with the profile `reranker`. |
| `shellbox` | `celine-shellbox` | Celine's own Debian computer, where its terminals run. |
| `browser` | `celine-browser` | Celine's own Brave on a screen of its own, with its home. |

## Profiles

`COMPOSE_PROFILES` in `.env` picks the optional services: `shellbox` (its computer and its
morning reset), `browser`, `discord`, `whatsapp`, `web` (SearXNG) and `reranker`.
`.env.example` lists the first five. The app starts with none of them; a service you do
not start needs none of its keys, and its feature can be switched off in Settings,
Features (docs/customize.md). `SHELLBOX_RESET_AT` (HH:MM in `TZ`, default `04:00`) is
the hour of the shellbox's daily reset, run by the `shellbox-manager` service of the
`shellbox` profile.

Every long-running service restarts on its own (`unless-stopped`) and has a health
check; `docker compose ps` shows the result. Only `celine` publishes a port; the others
are reached by service name on the project's network.

## First start

Needs Docker Engine 26 or newer with the compose plugin (each service mounts a folder
of the volume, which older engines cannot do).

```sh
cd celine-net
docker compose up -d      # builds the images the first time
docker compose ps         # every service turns "healthy" within a minute
```

Nothing to type first: `.env` is optional (`.env.example` lists what it may set, the
profiles, the published port, the time zone). The stack makes its own keys: the app,
at its start and before it reports healthy, writes the volume's `app/keys/` once, `shell.key`, `browser.key`, `connector.key`, `shellbox-llm.key` and
`searxng.key` (mode 600), and what the other containers get instead of a key:
`shell.hash` and `browser.hash` (the sha256 the boxes check), `connector-clients` (the
connectors' list of clients, `live:<key>:owner`) and `searxng-settings.yml`
(`config/searxng/settings.yml` with `server.secret_key` filled). An existing key is
kept; delete one and the next start makes a new one and its derived file. Without a
profile you start fewer services, never fewer keys.

Add your passkey (see Login): open http://localhost:8766 (not 127.0.0.1, which passkeys
do not accept), enter the setup code (`docker compose exec celine cat /data/app/setup-code.txt`) and add the
passkey. A new install has no models, no accounts and an empty archive. Set it up in
the app, in this order:

1. **Settings, General.** Your name (what Celine calls you; `{{username}}` in its
   instructions) and your time zone (set `TZ` in `.env` to the same zone).
2. **Settings, Models.** Add the server your models run on (llama-swap, Ollama,
   llama-server, or a cloud provider with its key), then pick models for the roles
   (Chat, Titles and descriptions, Topic Search, Reranking, Photo descriptions). A role
   may list models on several servers: the first that answers does the work. Until a
   chat model is set, a message waits in the queue with one line saying so.
3. **Settings, Discord.** "Log in with QR code", then scan the code in the Discord app
   on your phone (Settings, Scan QR Code). Pick the DMs and servers to keep.
   Logging in with a user account may break Discord's terms; use it at your own risk.
4. **Settings, WhatsApp.** Scan the code shown there on your phone (WhatsApp,
   Settings, Linked devices, Link a device). The phone then sends its history.
   It is an unofficial client, which WhatsApp does not allow and may ban the number
   for; use it at your own risk.

A connector with no internet access is a normal state: the source page says so, and
"Sync now" catches up by hand once it is back.

### Images from a registry

To pull prebuilt images instead of building: set `CELINE_IMAGES` (for example
`registry.example.net/you/celine-net`) and `CELINE_TAG` in `.env`, `docker login`, then
`docker compose pull && docker compose up -d --no-build`. The machine then needs only
`docker-compose.yml`, `.env` and `config/searxng/settings.yml`.

## Where the data lives

Everything is on one Docker volume, `data` (named `<project>_data` by Docker, for
example `celine-net_data`; `docker volume ls` shows it). The app mounts the whole
volume (it creates the folders at its start); every other service mounts only its own
folder.

| Folder | Mounted in | Holds |
|---|---|---|
| `app/` | `celine` at `/data/app` (the volume at `/data`) | `celine.db` (conversations, settings state), `log.db` (events, requests), `kb/` (the archive: one database per source, Documents), `notebook/` (Celine's notebook and its index), `documents/` and `blobs/` (files), `settings/` (what Settings saves), `secrets/` (keys typed into Settings, mode 600), `terminals/` (everything its terminals printed), `logs/` |
| `discord-link/` | `discord-link` at `/data` | `token` (the Discord login, mode 600), `state.db` (the journal, the chosen DMs and servers), `media/`, `lock` |
| `wa-link/` | `wa-link` at `/data` | `store.db` (the linked device), `state/` (the journal, media, lock) |
| `shellbox/` | `shellbox` at `/home/celine` | Celine's home in its shellbox: its notes, scripts, what it installed. A backup can leave it out. |
| `reranker/` | `reranker` at `/models` | Only with the profile `reranker`: the model, downloaded on first start (about 0.6 GB). Nothing private; a backup can leave it out. |

Nothing secret is in an image or in the compose file. The keys in `.env` reach the
containers as environment variables; everything typed into the app stays on the volume.
The passkeys, sessions and recovery codes are in `app/auth.db` (mode 600). The app's
folder (`app/`, or `var/` without Docker) is mode 700 and what the app writes there is
its user's alone (umask 077); the files other containers mount from `app/keys/` keep
their own modes.

Hand-written configuration (`config/*.toml`, its shipped base in `config/her/` and the
schedules copied once into `app/settings/`) is part of the app image. Settings changed
in the app go to `app/settings/` and `app/secrets/`.

## Backups

The app keeps its own nightly backups every night at 01:00 in its time zone
(`server/celine/backups.py`): its notebook as `backups/notebook/<night>.tar.xz`
and the database as `backups/db/<night>.db.xz` in the data volume. It keeps the last 7
nights.

For a full copy, stop the stack (the databases are SQLite in WAL mode), archive the
volume, start again:

```sh
docker compose stop
docker run --rm -v celine-net_data:/data:ro -v "$PWD":/backup alpine:3.24.2 \
    tar czf /backup/celine-data-$(date +%F).tgz -C /data .
docker compose start
```

Restore into an empty volume the same way, with the stack stopped:

```sh
docker compose down                      # keeps the volume
docker volume rm celine-net_data         # only when you replace it completely
docker run --rm -v celine-net_data:/data -v "$PWD":/backup alpine:3.24.2 \
    tar xzf /backup/celine-data-<date>.tgz -C /data --numeric-owner
docker compose up -d
```

The archive holds your whole chat history, the Discord login and the WhatsApp device.
Keep it as private as the machine itself. Never restore `wa-link/` while another copy
of that device runs somewhere else: WhatsApp allows one process per linked device.

## Updating

Back up first: a new version may migrate the databases, and an older version refuses
a database a newer one has migrated.

```sh
git pull && docker compose up -d --build          # built here
docker compose pull && docker compose up -d       # from the registry
```

The volume is kept. Migrations run at startup. The connectors keep their logins.

### Announcing a restart

A host script can tell everyone working that the app restarts, on the intercom. Make a
named token (Settings, Security, Tokens; shown once); a message sent with it comes from
`system:<name>`.

```sh
curl -fsS -X POST https://celine.example.net/api/intercom -H "Authorization: Bearer $CELINE_TOKEN" -H "Content-Type: application/json" -d '{"to": "all", "text": "The app restarts for an update now.", "until": "15 min"}'
```

`until` (a time like `04:00` or a span like `15 min`) makes it an announcement: every
turn that starts before then sees it.

## What the compose file does

| Setting | Why |
|---|---|
| `celine` mounts the whole volume | At its start, as root, the app creates the folders the other services mount (Docker mounts only a folder that exists) and gives `shellbox/` to its uid, then runs as `celine` with no capabilities, its data in `app/`. It makes the stack's keys (see First start) before it reports healthy. |
| `depends_on: celine: service_healthy` | Every other service that mounts a folder or a key file of the volume starts once the app is healthy, a few seconds after it starts. `docker compose ps -a` shows only running services. |
| key files from a volume subpath | The boxes, the connectors and SearXNG mount one derived file each, read-only, from `app/keys/`. |
| `CELINE_PUBLISH` | Where the app's port is published. Default `127.0.0.1:8766`, this machine only. `8766` or `<address>:8766` opens it to the network; then set `CELINE_PUBLIC_ORIGIN` too. |
| `CELINE_PUBLIC_ORIGIN` | The URL the browser uses, default `http://localhost:8766`. Passkeys are bound to its host name. |
| `CELINE_TRUSTED_PROXIES` | The reverse proxy's addresses or networks, comma separated. Only these may forward the client's address. Default: none. |
| `CELINE_LOGIN=on`, `CELINE_HEALTH_PORT=8765` (in the image) | The login is always on in the container. The health check asks port 8765 on the container's loopback, which nothing outside can reach. |
| `CELINE_METRICS_PORT=8772` (in the image) | Prometheus numbers at `GET /metrics` on their own port, loopback only. See Monitoring. |
| `CELINE_IMAGES`, `CELINE_TAG` | Where the images come from and which tag. Default: built here as `celine/celine-*:latest`. |
| `TZ` | The containers' time zone, default UTC: log lines, the Discord connector's dates and the app's day and hour counts. Set it to the zone chosen in Settings, General. |
| `CELINE_SHELL_URL`, `CELINE_BROWSER_URL`, `CELINE_SEARXNG_URL` | Where the app reaches the shellbox, the browser and SearXNG, the first time: they seed Settings, Features (`var/settings/services.toml`), which wins once saved. It calls the boxes with `keys/shell.key` and `keys/browser.key`. |
| `/etc/shellbox/llm.key` (the shellbox, from `app/keys/shellbox-llm.key`) | The model proxy's key, the app's and the shellbox's (with `SHELLBOX_LLM_URL`, `http://celine:8766/llm`): its agent CLIs call its models with it. Not a provider key. |

## Shellbox

Celine's terminals live in `shellbox`, a Debian container that is its own to play in. It
is the user `celine` with passwordless sudo, so it can install anything with apt. The agent's
uid and gid, 1000, are named once, `x-her-uid` in docker-compose.yml: both boxes are
built with it and the app gives its home to it at its start. A home that is still not its own (a
home disk, a restored folder) is given to it at the box's start, said in its log.
`shellbox/shellboxd.py` runs as root (nothing it does stops it), writes in its home as
it (nothing in it is root's), keeps its terminals as tmux sessions that run as it,
and gives the app a small API with `keys/shell.key` as the bearer token. The box gets
only the key's sha256, `/etc/shellbox/key.hash`, mounted read-only from the volume: it
has sudo, and root reads every environment and file in there, so the key itself never
enters the box.

Calls: `/run`, `/type`, `/stop`, `/read`, `/control` (your Stop, Pause, Resume, Close), `/keys` (your own keystrokes), `/view` and `/log` (live and raw bytes), `/file` (its files, byte ranges too), `/forward` (a raw tunnel to a port in its box, for its shares), `/code/*` (its coding agent's sessions), `/status`, and `/health` (no key; the compose health check asks it).

The agent's coding agent (the `code` skill) is opencode's headless server, the pinned release
binary in the image, started by shellboxd as it on `127.0.0.1:4096` and restarted when
it exits (its output in `/var/lib/shellbox/opencode.log`). Its password lives in its
home (`~/.config/opencode/server.password`, made at the first boot, kept across
resets); the app never holds it, shellboxd proxies `/code/*` to it. Its config, `~/.config/opencode/opencode.json`, is written at every boot: one
provider, `celine`, on the app's model proxy with every model the proxy offers, each with
the context and output limit the proxy reports (`context_length`, `max_output_tokens`;
the output a quarter of the context when it names none). The agent's own settings go in
`~/.config/opencode/opencode.jsonc`, which opencode reads after it and the box never
writes again. The config also names REA (`rea mcp`) as an MCP server. Every `/code/*`
error carries opencode's own status and words, or what the box saw (no answer on the
port, the server's exit code), with the log's path. The backend is one module in `shellbox/agents/`; `CODING_AGENT` names it (default `opencode`).

Every terminal's output is logged in the box as it comes, in
`/var/lib/shellbox/logs/<id>.raw`: root's, outside its `/tmp`, moved aside and gzipped
past 64 MB, never deleted. Each run of its own leaves `/var/lib/shellbox/cmd/<id>.start`,
`.log` and `.exit`. The app follows the listing every 5 s
(`server/celine/terminals/sync.py`) for the Terminals page and copies the new bytes of
each terminal's log into `app/terminals/<id>.raw`, gzipped when the terminal ends. It
deletes none of them: the page shows a terminal's whole history, after a reset too.
Your own terminals (New terminal there) run as celine in its home, named after you and
with a prompt that says so.

- Only its home, `/home/celine` on the volume's `shellbox/` folder, is kept. pip and
  npm install into `~/.local` there. Everything else is the image.
- None of the app's data is in there: no archive, no notebook, no keys, no login.
- It keeps Docker's default capabilities and allows setuid, because sudo needs both.
  `mem_limit`, `pids_limit` and `cpus` keep it from taking the machine down; `/tmp` is
  a 2 GB tmpfs that counts against its memory.
- Its own network: the app reaches it; the connectors and the search engine do not. The internet and your LAN are open to it.
  If it can do without them, `internal: true` is the recommended setting: uncomment it under `networks: shellbox` (and
  `browser`) in the compose file: the box then reaches only the app, so something that
  talked it into a command cannot reach your LAN or the internet from there. It can no
  longer install packages or fetch pages from the box then.

Every morning the shellbox goes back to a fresh image: recreating the container throws
away everything outside its home. The `shellbox-manager` service does it, inside the
stack, so the host needs nothing: at 04:00 (in `TZ`) it runs `docker compose up -d
--force-recreate --no-deps shellbox` on the stack folder (mounted read-only) through the
Docker socket. It holds the Docker socket (root on the host), so it has no network and
runs nothing but that. The agent's next result in a terminal says the shellbox was reset. A
restart, a crash, or `sudo reboot` inside the box keeps the container as it was; only
the recreate resets it. `docker compose logs shellbox-manager` shows what each morning
did.

Before the reset it keeps a snapshot of its home, one folder per day in `shellbox-snapshots/<date>` (hardlinks to the day before; a restore is by hand). `SHELLBOX_SNAPSHOTS` (a host folder) sets where, `SHELLBOX_SNAPSHOT_KEEP_DAYS` how many days they are kept (default 7; 0 keeps every one).

The agent's home's size: `SHELLBOX_HOME_GB` gives it a hard cap (default 50 GB),
`SHELLBOX_HOME_SOFT_GB` a soft one (default 90% of the hard cap). The box measures its home every 10 minutes;
past either cap, its next result says so and asks it to clean up or ask for more room,
and past the hard cap the box takes no more uploads. What the agent writes from its own
shell does not pass through the box's daemon: only the disk below stops that.

A hard limit, optionally: with `SHELLBOX_HOME` (a host folder) as well, its home can be an ext4 disk image of that size (`$SHELLBOX_HOME.img`, mounted by `/etc/fstab`). Replace the shellbox's home volume in the compose file with `- ${SHELLBOX_HOME}:/home/celine`, then, as root in the stack folder, `docker compose stop shellbox`, `shellbox/home-disk.sh` (makes the disk, moves the files in, adds the fstab line), `docker compose up -d shellbox`. Run it again after raising `SHELLBOX_HOME_GB`: the disk grows in place; shrinking is by hand. The folder under the disk is made immutable (`chattr +i`): if the disk ever fails to mount, its box refuses to start (shellboxd cannot write its home) instead of writing onto the host's disk; the rest of the stack keeps running.

### Agent CLIs

The image carries opencode, and `agent` (shellbox/agent.py), which runs it headless on
one task (and runs the `cli` sub-agents), REA (`rea-agents`, pinned: the `rea` CLI and its
MCP server, which opencode's config names) and the Anatomy skill in `/opt/skills/anatomy`
(Chromium for its checks). opencode, and any CLI it installs, calls its models through the app's model proxy,
`/llm/v1/chat/completions` and `/llm/v1/models` (server/celine/api/llm_proxy.py):
shellboxd puts `OPENAI_BASE_URL` and `OPENAI_API_KEY`
into its terminals from `SHELLBOX_LLM_URL` and `/etc/shellbox/llm.key`. The proxy opens every
model in Settings that can chat and has its key (`settings/her_models.py`), and holds the
provider keys itself. Every call takes its slot in the app's model queue in the
`shellbox` lane at background priority. The key opens only /llm/v1: on /api it is no
token, so the box reaches no guard or approval route; while it is frozen the proxy
answers 403.

### Environment

The two boxes read these; the compose file sets the ones it names, `.env` the ones with a
`${}` there.

| Name | Default | What |
|---|---|---|
| `BROWSER_KEY_HASH_FILE` | `/etc/browser/key.hash` | the sha256 of the app's `keys/browser.key`; the browser box accepts only a call with that key |
| `BROWSER_PORT` | `8771` | the port browserd listens on |
| `SHELLBOX_LLM_URL` | none | the app's model proxy its terminals call (`OPENAI_BASE_URL`) |
| `SHELLBOX_HOME_GB` | `50` | its home's hard cap in GB: past it no upload lands |
| `SHELLBOX_HOME_SOFT_GB` | 90% of the hard cap | past it the agent is told to clean up |
| `SHELLBOX_RESET_AT` | `04:00` | the hour of the morning reset, HH:MM in `TZ` |
| `SHELLBOX_SNAPSHOT_KEEP_DAYS` | `7` | days its home's snapshots are kept (shellbox-manager); 0 keeps every one |

## Browser

Celine's browser lives in `browser`, a Debian box with Brave. `browser/run.sh` keeps
`browser/browserd.py` running; browserd starts Xvnc (the screen, whose page is
1440x900) and Brave on it, drives Brave over the DevTools protocol on the box's own
loopback, and gives the app a small API with `keys/browser.key` as the bearer token. The
box holds only its sha256, `/etc/browser/key.hash`.

Calls: `POST /do` (one action in a named tab, or a restart), `POST /control` (take over, give back, restart), `GET /status`, `GET /health` (no key).

- Everything runs as celine (`x-her-uid`, as in its shellbox) with its home, the
  volume's `shellbox/` folder: the profile is `~/.browser`, downloads land in `~/Downloads`, where its shellbox sees
  them. With `SHELLBOX_HOME` (see Shellbox), mount that folder here too.
- The agent's extensions: every folder in `~/.browser-extensions` with a `manifest.json`; each
  start loads them. A broken one hangs Brave's start, so the second launch goes without
  them, and its next result says so.
- `seccomp:unconfined`, because Brave's own sandbox needs user namespaces, which
  Docker's seccomp profile blocks; nobody has a shell in this box. Never
  `apparmor:unconfined`: where unprivileged user namespaces are restricted (Ubuntu
  24.04), Docker's default AppArmor profile is what lets Brave make them.
- 3 GB, 2 CPUs, 1024 processes; `/tmp` is a 1 GB tmpfs inside that.
- When Brave or Xvnc dies or stops answering, browserd exits; `run.sh` starts it again
  a second later, and kills a browserd whose `/health` stays silent for 10 s. Every start
  reopens its tabs at their last addresses (`~/.browser/celine-tabs.json`; not its
  throwaway tabs); its next result says so.
- Its own network: only the app reaches it, no port is published. The internet and the
  LAN are open to it. The shellbox's morning reset leaves it running.
- The browser page's live view: the app relays Xvnc (port 5900 on that network, no
  password, since only the app is on it) to noVNC in the page. Xvnc takes keys, mouse
  and pasted text only while you have taken the browser.

## Web search (SearXNG)

Celine's `web` skill searches through a private [SearXNG](https://docs.searxng.org)
(service `searxng`): a metasearch engine that asks the public engines on its behalf,
with no API keys, no account and no cookies. Pages are fetched by the app container
itself and the text is extracted locally (trafilatura, pypdf).

`./config/searxng/settings.yml` is mounted read-only: `formats: [html, json]` (Celine
reads json), `limiter: false` (one client), no image proxy, 4 s per engine. The service
runs with `FORCE_OWNERSHIP: "false"` and `cap_drop: [ALL]`.

The app reaches it as `http://searxng:8080` (`CELINE_SEARXNG_URL`, the seed of the
address in Settings, Features, Web search). Without it the address is
`http://127.0.0.1:8888` until you save one.

**Privacy.** What leaves the stack depends on where your models run. With local
models only, web search is the one feature that sends something out. With a cloud
provider in any role, that provider sees what the role reads: for Chat, the persona,
its notes, archive excerpts (other people's messages too), mail, shell output and
screenshots; for the other roles, the text or image each one is given. OpenRouter
requests carry `data_collection: "deny"` unless you change it, and each OpenRouter model
can ask for zero data retention (Settings, Models). Keep archive content local by
giving Chat and Topic Search local models.

A search query goes (through SearXNG) to the public engines, and `web__read_page` gets
the page from its server with a User-Agent that names Celine as a self-hosted personal
assistant. Celine is told
never to put names, numbers or message text from the chat archive, Documents or its
notebook into a query or URL unless you asked it to. Turn the skill off with `[web]
enabled = false`, or per conversation in its Skills panel. `web__read_page` refuses
anything but public http(s): `file://`, loopback, private LAN ranges, link-local, CGNAT
and `.local`-style names are refused, redirects are checked hop by hop, and the address
actually connected to is checked again (DNS rebinding). Limits: 20 s per fetch, 5 MB of
HTML, 25 MB of PDF, 5 redirects.

## Reranker

Archive search (Celine's `find`) orders its best candidates with a cross-encoder,
bge-reranker-v2-m3, served by llama.cpp's `llama-server --reranking`, which speaks the
`/v1/rerank` API the app uses and downloads the model itself.

- **In this stack**, service `reranker`, with the profile `reranker`
  (`COMPOSE_PROFILES=reranker` in `.env`). Without the profile the service does not
  start.
- **First start.** The container downloads `gpustack/bge-reranker-v2-m3-GGUF` (Q8_0,
  about 0.6 GB) from Hugging Face into the volume's `reranker/` folder, then serves it;
  `docker compose ps` shows "starting" meanwhile. To use another file of the model, set
  `RERANKER_MODEL` in `.env` (for example `gpustack/bge-reranker-v2-m3-GGUF:Q4_0`,
  faster on a CPU, slightly less exact) and delete the old file from the folder.
- **In the app.** Settings, Models: add a server of kind llama-server at
  `reranker:8080`, then put `bge-reranker-v2-m3` in the Reranking role. With the role
  empty, or the reranker down, search still works and keeps the order of the word search
  and the semantic topic search.
- **CPU or GPU.** The service runs on the CPU. With an NVIDIA GPU and the NVIDIA
  Container Toolkit, start the stack with the GPU file and the same service runs on the
  GPU:

  ```sh
  docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
  ```

To run it on a GPU machine elsewhere instead, copy the `reranker` service into a
compose file of its own there (a CUDA `ghcr.io/ggml-org/llama.cpp:server-cuda-*` image,
`--reranking`, `--alias bge-reranker-v2-m3`, the nvidia device reservation), leave the
profile out here, publish its port only on a LAN address (llama-server has no key), and
add a llama-server provider at that address in Settings, Models.

## Login

The login is a passkey (recommended), a password or an 8-digit PIN. `CELINE_LOGIN=on`
turns it on (the image does); off is for local runs, and then the server refuses to bind
anything but loopback, so a forgotten setting can never open it to a network.

With the login on and no session, every route answers 404 exactly like an unknown
route: the API, the WebSockets, the app's pages and files. The only exceptions are the
login screen at `/` and the six login calls it makes.

**First start.** With no way to log in yet, the app writes a one-time setup code to
`<data>/setup-code.txt` (mode 600; in the stack `docker compose exec celine cat
/data/app/setup-code.txt`). The log only says that the code is there, never the code. Open the app, enter the code and pick
a method: add a passkey, set a password (at least 10 characters) or a PIN (exactly 8
digits). Then the screen shows ten recovery codes once: save them. Every restart without
a login writes a new setup code, and the old one stops working.

**Local development** runs with the login off. To try the login locally, set
`CELINE_LOGIN=on` and open http://localhost:8766. Use `localhost`, not 127.0.0.1.

**Recovery.** On the login screen, "Use a recovery code" takes a code and adds a new
way to log in, any of the three. Each code works once. Settings, Security lists the
passkeys, passwords and PINs, adds and removes them (never the last one; removing one
ends its sessions), and makes new recovery codes (the old ones stop working). Adding a
way to log in, new recovery codes and a new API token ask you to confirm it is you
(a passkey, your password or your PIN) unless you did in the last 5 minutes; logging in
counts. It also lists the browsers logged in, each with when it logged in and when it
was last seen, and logs out one or every other.

**Stored** (`auth.db`, mode 600): the passkeys' public keys, the passwords and PINs as scrypt hashes, a SHA-256 of each session token with its browser's user agent (trimmed), and the recovery and setup codes as scrypt hashes. **Sessions** end 30 days after last use and 90 days after login; the cookie is `HttpOnly`, `SameSite=Strict`, and `Secure` on https. **Limits:** two locks, each on its own: after 5 wrong setup or recovery codes, code entry locks for everyone for 6 hours, and after 5 wrong passwords or PINs from one client address, logging in with them from that address does (so nobody can lock you out from elsewhere); each further wrong one doubles that lock up to 30 days. A locked kind refuses even the right one, while the other kind still works. Failed logins of any kind are also limited to 10 per client address in 15 minutes.

## Behind a reverse proxy (https)

Passkeys belong to the public address, so name it; it is never guessed from request
headers:

```sh
# .env
CELINE_PUBLIC_ORIGIN=https://celine.example.net
CELINE_PUBLISH=192.168.1.5:8766         # where the proxy reaches the app
CELINE_TRUSTED_PROXIES=192.168.1.2      # the proxy's address (or network, 192.168.1.0/24)
```

The proxy must pass the `Host` header and upgrade WebSockets (`/ws/events`,
`/ws/terminals/*`, `/ws/browser`); Traefik and Caddy do both by default.
Point the router for `celine.example.net` at `http://192.168.1.5:8766`.

The app reads the client's address from `X-Forwarded-For` only when the request comes
from `CELINE_TRUSTED_PROXIES` (used for the login limit and the log). A request from
any other address that carries `X-Forwarded-For`, `Forwarded` or `X-Real-IP` is logged
once as a warning: every client behind that proxy counts as its one address. Behind two
proxies in a row, list both. Changing the public address later means registering the
passkeys again. `[server] allowed_hosts` in `config/celine.toml` adds more host names
for the `Host` check.

The server accepts a request only if its `Origin` and `Host` headers name it. Chat files open in the browser only as pictures, video or audio, in a sandbox; the rest is a download. Celine's artifact pages (`/api/artifacts/<id>/versions/<n>`) run their scripts in a sandbox with an origin of their own.

## Connectors

The app never talks to Discord or WhatsApp itself. `discord-link` and `wa-link` own the
accounts, keep a journal of everything they learn and serve three doors behind bearer
keys: Celine's read tools (`/mcp`), the journal (`/feed`) and `/admin`. Outside Docker
the app starts each one when its address does not answer (`config/mcp.toml`); the
process keeps running when the app stops. In Docker they are services of their own.

- Keys: the clients file (`-clients-file`, in Docker `/data/connector-clients`, the
  app's `keys/connector-clients` mounted read-only) lists who may connect, `name:key:role`
  one per line or comma separated. `owner` may change the account and delete chats;
  `reader` may only read (a second app instance). The app uses `keys/connector.key`,
  which `celine keys` writes into that file as `live`. The connectors' health checks use
  the first key in the file.
- Only one process may use a WhatsApp device. wa-link locks its state folder, so a
  second copy on the same folder exits at once. Never run a second copy of the same
  device on another machine either: stop the old one before the new one starts.
- wa-link's limits: `WA_LINK_MEM_LIMIT` in `.env` (default `2g`) is its container's
  memory; `WA_LINK_MAX_MEDIA_MB` (default `100`, the flag `-max-media-mb` outside Docker)
  is the largest attachment it downloads for the app. A larger one is refused before it
  is downloaded, by the size its message names.

## CI

GitLab CI: `.gitlab-ci.yml` runs on master (and on "Run pipeline" by hand); pushes to
other branches build nothing. A push runs only the jobs whose files changed.

- Tests: `test:python` (pytest, the app and discord-link), `test:wa-link` (`go test ./...`)
  and `test:web` (the web build, which type checks first).
- Images: `build:app`, `build:discord-link`, `build:wa-link`, `build:shellbox`,
  `build:shellbox-manager` and `build:browser` build one image per Dockerfile, each after
  the tests of what it ships, and push it to the project's registry as
  `$CI_REGISTRY_IMAGE/celine-*`, tagged with the branch or tag slug and `:latest` on
  master.

GitHub Actions: `.github/workflows/test.yml` runs the gates on every push and pull
request; `images.yml` builds the six images on master and on a `v*` tag and pushes them
to `ghcr.io/<owner>/celine-net/celine-*`, tagged `latest` (master), the tag as pushed
(`v1.2`) and the commit. `CELINE_TAG=v1.2` keeps a machine on a release; `CHANGELOG.md`
says what each one changed.

To pull them, set `CELINE_IMAGES=<registry>` in `.env` (the project's
`$CI_REGISTRY_IMAGE`, for example `registry.example.net/you/celine-net`) and
`CELINE_TAG` when not `latest`, then `docker login <registry>`. To build one yourself
from the repository root: `docker build -f wa-link/Dockerfile .`. After changing the
dependencies of discord-link, run `uv lock` in `connectors/discord`.

## Monitoring

The app counts turns, tool calls, model calls, Routines runs, the `/llm/v1` proxy,
sub-agents and the guard's decisions (server/celine/metrics.py, names `celine_*`). It
serves them at `GET /metrics` on `CELINE_METRICS_PORT` (8772 in the image; unset
locally: no listener), never on the app's port. It listens on the container's loopback
only, like the health port, so the shellbox and the browser (which share a network with
the app) cannot read it, and a request carrying `X-Forwarded-For`, `Forwarded` or
`X-Real-IP` gets 403 there. To scrape it, run Prometheus (or an exporter) in the app's
network namespace (`network_mode: service:celine`) and point it at `127.0.0.1:8772`.

## Logs

`docker compose logs -f celine` shows the app's JSON logs; `docker compose logs -f discord-link wa-link` the connectors'. Health checks run every 30 s (`docker compose ps`).
