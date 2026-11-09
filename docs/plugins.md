# Plugins

A plugin is a folder that adds one feature to celine-net: a switch in Settings, Features,
plus any of these: tools for the agent, MCP servers, a settings page, HTTP routes, a
background task, its own SQLite file, a device for sub-agents and places in the rail.

- `plugins/mac/` ships with the app: the agent's own Mac.
- `examples/plugins/echo/`: a Python skill with a settings page, a route and a database.
- `examples/plugins/remote/`: an MCP server filled from its settings page.

The tests load both examples, so they match the current app.

## Where plugins live

| Folder | What |
|---|---|
| `plugins/<id>/` | the plugins that ship with the app |
| your plugins folder | `CELINE_PLUGINS_DIR`, or `paths.plugins` in `config/celine.toml`, default `<data>/plugins/`. With Docker compose it is `./user-plugins` beside `docker-compose.yml`, mounted read-only at `/plugins` |
| `<data>/plugins/<id>/` | each plugin's data: its SQLite file and what its code writes |

A plugin in your folder with the id of a shipped one replaces it. The app reads the
plugins at start, so a new or removed folder takes a restart. A new plugin starts
switched off, and Settings, Features switches it on and off while the app runs. Settings
lists your plugins under User modules, and a plugin that owns a place under Base modules.

## Three ways to give the agent something

- **An MCP server**, in any language. This is the way for code with packages of its own:
  run the server in its own container and connect over HTTP (`transport = "http"`). A
  stdio server runs inside the app's container with the programs it carries: Python with
  the app's packages.
- **A Python skill**: a class whose methods are the agent's tools
  (`examples/plugins/echo/skill.py`).
- **Routes and a background task**: a FastAPI router under `/api/plugins/<id>/` and a
  coroutine that runs while the plugin is on (`plugins/mac/gateway.py`).

## The folder

```
<id>/
  plugin.toml         the feature: its texts, skills, servers and routes
  settings.toml       its settings page
  __init__.py, *.py   its Python, the package celine_plugins.<id>
  migrations/         NNNN_<name>.sql, the schema of its SQLite file, applied in order
  guides/<skill>.md   the guide of an MCP server's skill, loaded with its tools
```

`plugin.toml` is the one required file. Its Python is imported when the plugin is switched on.
Switching it off removes its skills, routes and servers and closes its database.

## plugin.toml

`{{name}}` in a text is the agent's name.

| Key | What |
|---|---|
| `id` | the folder's name: lowercase letters, digits and `_`, starting with a letter |
| `label` | its name in Settings |
| `about` | one sentence on what it gives you, under its switch |
| `needs` | what turns it on, shown while its switch is greyed |
| `off_when` | "Turn it off if ...", its switch's tooltip |
| `start_on` | `true` switches it on at the first start |
| `warn` | a confirmation shown before its switch goes on |
| `group`, `choice` | the setup step it shares with its group (`"computer"`) and its heading there |
| `places` | the rail places it owns (`"terminals"`, `"browser"`); they show while it is on and set up |
| `skills` | `[skills.<name>]` with `class = "module:Class"`: a Python skill |
| `servers` | `[servers.<id>]`: an MCP server, keys below |
| `routes` | `["module:router"]`: FastAPI routers at `/api/plugins/<id>/` while it is on |
| `run` | `"module:coroutine"`, called as `run(services, plugin)` while it is on |
| `open` | routes open to a machine with a key of its own, such as `"POST /gateway"`; the route checks that key |
| `drones` | a device for sub-agents running opencode serve: `url`, `password`, `share`, `key`, `label`, `about` |

`${KEY}` in a server's or a drone's value is the value of the field `KEY` on its settings
page. An unknown key shows as a problem in Settings, General, and the plugin loads once
the key is fixed and the app restarts.

### A server

`[servers.<id>]` takes the keys of `config/mcp.toml`. Its tools are `<id>__<tool>`.

| Key | What |
|---|---|
| `enabled` | `true` starts it |
| `transport` | `"stdio"` or `"http"` |
| `command`, `args`, `env` | the stdio server's program, its arguments and its environment |
| `url`, `headers` | the HTTP server's address and the headers it gets |
| `startup_timeout_seconds` | how long its start may take, 20 by default |
| `max_restarts` | how many times the app starts a stopped stdio server again, 5 by default |
| `kb` | a connector's Messages archive, which its results and its feed fill |
| `class` | who may see its results, for Bastet: `public`, `informal`, `nsfw` or `sensitive` (the default) |
| `sends` | where its writing tools send, for Bastet: `public` (the default), `people` or `house` |
| `outside` | when its results count as outside text, for Bastet: `always` (the default) or `shell` |
| `words`, `groups` | the sentence of each tool's row in the chat, and of a run of them ([customize.md](customize.md#skills-and-mcp)) |
| `place`, `place_tools` | the place its calls show in (`"terminals"`: Commands, with their `command` or typed `input`; `"browser"`: History, with their words) and which of its tools, by default every tool in its `words` |

A plugin that owns such a place names it in `places`. Terminals and Browser stay in the
rail while the shellbox, the browser box or a plugin of theirs is on and set up.

## settings.toml

Its settings page uses the field schema of every settings page, described field by field
in `server/celine/settings/form.py`.

- `[[fields]]`: `key`, `label`, `type`, and by type `default`, `help`, `choices`, `min`,
  `max`, `max_len`, `required`, `filled`. The types are `bool`, `choice`, `number`,
  `duration`, `text`, `list`, `secret` and `copy`. Values are saved in
  `<data>/settings/<id>.toml`, a `secret` in `<data>/secrets/<id>.<key>.key`.
- `[[actions]]`: `id`, `label`, `run = "module:coroutine"` and an optional `input`: a
  button. `run(services, plugin, input)` returns `{"ok", "sentence"}` and optionally
  `"lines"`.
- `copies = "module:coroutine"`: `copies(services, plugin)` fills the page as it opens: a
  `copy` field's text by its key, `note` (the page's line), `hide` (the ids of the
  actions it holds back) and `badges` (`{action id: words}`, a mark on that button).
- `note`: a fixed line at the top of the page.

## Python

A skill, from `examples/plugins/echo/skill.py`:

```python
from celine.skills import ToolContext, ToolResult, described, guide, tool


class Echo:
    name = "echo"
    guide = guide("echo", file=__file__, rel="guide.md")
    description = described()

    def __init__(self, cfg, plugin):
        self.cfg, self.plugin = cfg, plugin

    @tool(read_only=False)
    async def say(self, ctx: ToolContext, text: str) -> ToolResult:
        """Says `text` back with the prefix of its settings, and keeps it."""
        ...
```

Each `@tool` method is the tool `<skill>__<method>`. The model reads its docstring and its
arguments. In the plugin's code:

- `plugin.db` is its SQLite file, built from `migrations/`. `read` and `write` take a
  function of a connection.
- `plugin.form` is its `settings.toml`, and `cfg.settings.plugins["<id>"]` its saved
  values.
- `plugin.data` is its data folder.

Plugins import the app's modules (`celine.skills`, `celine.settings.form`,
`celine.plugins`). The examples use them as the current app has them, and the tests run
the examples, so after an update the examples show what changed.
