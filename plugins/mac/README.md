# Her own computer: a Mac

A base plugin: the switch "Her Mac" under Base modules (Setup's Features step, and Settings,
Features); switched on in Setup, its form is the "Own computer" part of the "Her computer"
step, and a Settings page. `setup.sh` makes a Mac (Apple silicon, macOS 15 or
later) a computer she uses from the app. It installs with Homebrew and npm only (no Apple
Account):

| What | For |
|---|---|
| Xcode command line tools, Homebrew | the installs (Homebrew's installer brings the tools) |
| node | the servers |
| Google Chrome (brew cask) | the browser Playwright MCP drives, over its DevTools port |
| [Peekaboo](https://github.com/openclaw/Peekaboo) (brew) | `screen`: see, click, type, menus, windows, foreground included |
| [Desktop Commander](https://github.com/wonderwhy-er/DesktopCommanderMCP) | `shell`: commands, processes, files |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | `browser` |
| [MCP filesystem server](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem) | `files`: the whole disk; `read_media_file` puts any file into the chat |
| [Supergateway](https://github.com/supercorp-ai/supergateway) | the four at `http://<mac>:8765/{screen,shell,browser,files}/mcp` behind a bearer token |
| [opencode](https://opencode.ai) 1.19.4 (the release zip, its sha256 checked) | `opencode serve` at `http://<mac>:4096` for her drones, the token as its password |

What it sets on the Mac:

| Where | What |
|---|---|
| `/etc/sudoers.d/celine` | her user runs sudo without a password |
| `pmset -a displaysleep 0 disablesleep 1` | the display stays on, the Mac never sleeps, the lid closes without sleep |
| `sysadminctl -screenLock off` | the screen never locks |
| `~/.claude-server-commander/config.json` | Desktop Commander: `blockedCommands` empty, `fileWriteLineLimit` 100000 |
| `~/.config/celine/token` | the gateway's token (mode 600), made once |
| `~/.config/celine/app`, `~/.config/celine/link` | the app's address and the link key (mode 600), filled in by the setup line (or `--app` and `--key`) |
| `~/.config/celine/gateway.json` | the gateway's servers and settings, written on every run |
| `~/.local/share/celine` | the npm packages, and `chrome/`, Chrome's profile |
| `~/Library/LaunchAgents/com.celine.chrome.plist` | Chrome with DevTools on 127.0.0.1:9222; its tabs and logins outlive the browser server's sessions |
| `~/Applications/Celine.app`, `~/.local/share/celine/app` | the app every server runs under, so macOS's grants are its own (Permissions); its source |
| `~/Library/LaunchAgents/com.celine.gateway.plist` | the gateway under `caffeinate -d` while she is logged in |
| `~/.local/share/celine/opencode/opencode` | opencode |
| `~/.config/opencode/opencode.json` | its own keys set on every run, the rest kept: the app's model proxy as provider `celine` (`<app>/llm/v1`, the link key read from `~/.config/celine/link`), Peekaboo and Playwright MCP (the gateway's own copies, Playwright on the same Chrome), every permission allowed, no autoupdate; the app adds the models of `celine` to the same file (`PATCH /global/config`) while no drone works there |
| `~/Library/LaunchAgents/com.celine.opencode.plist` | `opencode serve` on 0.0.0.0:4096, logging to `~/Library/Logs/celine-opencode.log` |
| `~/.local/share/celine/share.js`, `~/Library/LaunchAgents/com.celine.share.plist` | her shares (`shell__share(device="mac")`) on 0.0.0.0:8767 behind the token: `GET /forward?port=` tunnels to a localhost port, `GET /file?path=` serves a file, as shellboxd does |

With the lid shut, a dummy HDMI plug gives the Mac the display its screenshots need.

In the app:

| What | Where |
|---|---|
| `plugin.toml` | the switch and the four servers, `macscreen`, `macshell`, `macbrowser`, `macfiles`, filled from the form: `${URL}` the Address, `${TOKEN}` the Token; `[drones]`, the device `mac` (server/celine/devices.py): `${AGENTS}` and the Token, `${SHARE}` its share server, the link key its model proxy key |
| `settings.toml` | the form: the setup line, Address, Agents, Shares, Token, Check |
| `gateway.py` | `GET /api/plugins/mac/setup/<token>` (the setup line's script), `GET /api/plugins/mac/setup.sh`, `POST /api/plugins/mac/gateway` (the report), `POST /api/plugins/mac/events` (her scripts), the setup line, Check |
| `guides/` | each server's guide |

## Run

With the switch on, the form shows the setup line (and she gets it from
`settings__read` while the Mac is not connected). Logged in at the Mac as her (an admin),
in Terminal:

```sh
curl -fsSL http://<celine>/api/plugins/mac/setup/<token> | bash
```

The token works once within an hour; each look at the form makes a new line. The script
it gets has the app's address (Settings, General, Network) and the link key filled in, and
reads her password from the terminal, once for sudo and once for the screen lock. At its
end it posts its report to the app, which fills the form (the Mac's addresses, its facts
and the token) and starts the four servers; the form then shows Connected with the Mac's
facts. While the app's address is localhost the form asks for its network address in
place.

```sh
bash setup.sh            # install or update; ends with the address and the token
bash setup.sh --json     # the same, the report as one JSON line
bash setup.sh --status   # only the report, nothing installed
```

The report names each server's tool count (`null` when it does not answer) and the macOS
permissions it lacks. A second run updates the gateway's config and restarts it and
Chrome; the token stays.

The app reconnects to the four servers by itself while the Mac is away (a reboot waits
for FileVault's unlock and her login); the form says why they are down until then.

## From the Mac

`POST /api/plugins/mac/events` with the link key starts her turn with a message to herself
(`intercom__send` to "me"), in the conversation it names or her newest chat:

```sh
curl -fsS -H "Authorization: Bearer $(cat ~/.config/celine/link)" -H 'Content-Type: application/json' \
  -d '{"text": "the backup is done", "conversation": "<id>"}' "$(cat ~/.config/celine/app)/api/plugins/mac/events"
```

## Drones

`sub_agents__start(brief, name, device="mac")` starts a drone: opencode on the Mac works
on the brief in a session of its own, on her model through the app's model proxy (its
queue, freeze and usage), with Peekaboo and Playwright MCP, and reports like any
sub-agent (server/celine/sub_agents/drone.py).

## Permissions, once at the Mac

Peekaboo needs Screen Recording and Accessibility (which also covers what it reports as
Event Synthesizing). macOS asks them of the process a LaunchAgent starts, and every
process under it uses that one's grants. setup.sh builds `~/Applications/Celine.app`
(bundle id `com.celine.mac`, ad-hoc signed, rebuilt only when its source changes) and
runs the gateway, opencode and share.js under it, Peekaboo with `--no-remote` so it asks
in its own process. One app gets both grants, and upgrades of node, peekaboo or opencode
keep them. Under the gateway Celine asks for both once, which puts it into both lists;
at its end setup.sh opens the two panes one after the other and shows Celine in Finder to
drag in should it be missing.

Check on the form, its note, and her `settings__read(target="forms.mac")` name each
permission that is off and where to turn Celine on.
