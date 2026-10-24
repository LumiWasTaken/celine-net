# celine-net

A self-hosted AI agent with its own memory, schedule, Linux box and browser. One Python
process (FastAPI, SQLite), a React UI, and optional containers for the box, the browser,
a SearXNG, a Discord and a WhatsApp connector, a reranker. Models come from your own
servers (llama.cpp, llama-swap, Ollama, tabbyAPI, any OpenAI-compatible server) or from
OpenRouter and Anthropic with a key.

What it does: chats with branches and a plan per conversation; a notebook the agent keeps
on its own; routines on a schedule; missions over days with sub-agents; an intercom
between you, the agent and its work; terminals in its own Linux container; a browser of
its own with a live view; images and music through ComfyUI; a searchable archive of your
Discord and WhatsApp history and your documents; a guard model that checks risky actions.
Optional [dictation and read-aloud](docs/speech.md) use your OpenAI-compatible speech
providers, configured in Settings or during onboarding. No speech engine is bundled.

## Run

```sh
docker compose up -d
docker compose exec celine cat /data/app/setup-code.txt
```

Open http://localhost:8766, enter the setup code, pick a login (passkey, password or
PIN), then follow the setup: the agent, you, a model, and one card per feature. The
stack makes its own keys inside the volume; `.env` holds only profiles and the public
origin (see `.env.example`). Containers for the box, the browser, SearXNG and the
connectors are compose profiles: `COMPOSE_PROFILES=shellbox,browser,discord,whatsapp,web`.

[docs/DEPLOY.md](docs/DEPLOY.md): services, data, backups, updates, reverse proxy.

## Develop

Python 3.14 with [uv](https://docs.astral.sh/uv/), Node 22, Go for the WhatsApp connector.

```sh
./start.sh            # builds the UI when it changed, serves http://127.0.0.1:8766
./start.sh dev        # API plus the Vite dev server on http://127.0.0.1:5174
```

[docs/dev.md](docs/dev.md) has the gates, the switches, migrations and test rules.
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains a turn, the skills, the storage
and the routes. [docs/customize.md](docs/customize.md) lists the config files.
[AGENT.md](AGENT.md) is the map of the code and the rules it follows.

## Layout

| Path | What |
|---|---|
| `server/celine/` | the app: turns, skills, archive, settings, auth, routes |
| `web/` | the UI |
| `connectors/discord/`, `wa-link/` | the Discord (Python) and WhatsApp (Go) connectors |
| `shellbox/`, `shellbox-manager/` | the agent's Linux container and its reset |
| `browser/` | the agent's browser (Brave, browserd) |
| `config/` | shipped config: skills, MCP servers, sources, schedules, persona, presets |
| `tests/` | pytest on fakes |

## License

AGPL-3.0, see [LICENSE](LICENSE).
