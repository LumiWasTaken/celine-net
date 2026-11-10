# Its own computer: a Mac

A base plugin. Turn on `<name>'s Mac` (its name) under Built-in in Settings, Features (or the Features
step of Setup). Its form then appears as "Own computer" in the `<name>'s computer` step of Setup
and as its own Settings page. `setup.sh` makes a Mac (Apple silicon, macOS 15 or
later) a computer the agent uses from the app. It installs with Homebrew and npm only (no Apple
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
| [opencode](https://opencode.ai) 1.19.4 (the release zip, its sha256 checked) | `opencode serve` at `http://<mac>:4096` for the agent's drones, the token as its password |

What it sets on the Mac:

| Where | What |
|---|---|
| `/etc/sudoers.d/celine` | the agent's user runs sudo without a password |
| `pmset -a displaysleep 0 disablesleep 1` | the display stays on, the Mac never sleeps, the lid closes without sleep |
| `sysadminctl -screenLock off` | the screen never locks |
| `~/.claude-server-commander/config.json` | Desktop Commander: `blockedCommands` empty, `fileWriteLineLimit` 100000 |
| `~/.config/celine/token` | the gateway's token (mode 600), made once |
| `~/.config/celine/app`, `~/.config/celine/link` | the app's address and the link key (mode 600), filled in by the setup command (or `--app` and `--key`) |
| `~/.config/celine/env`, a line in `~/.zshenv` | `OPENAI_BASE_URL` (`<app>/llm/v1`) and `OPENAI_API_KEY` (the link key) for every zsh command on the Mac, so the agent's shell there calls the model proxy like the box's terminals |
| `~/.config/celine/gateway.json` | the gateway's servers and settings, written on every run |
| `~/.local/share/celine` | the npm packages, and `chrome/`, Chrome's profile |
| `~/Library/LaunchAgents/com.celine.chrome.plist` | Chrome with DevTools on 127.0.0.1:9222; its tabs and logins outlive the browser server's sessions |
| `~/Applications/Celine.app`, `~/.local/share/celine/app` | the app every server runs under, so macOS's grants are its own (Permissions); its source |
| `~/.local/share/celine/signing.keychain-db`, `~/.config/celine/signing-password` | the keychain with Celine.app's signing certificate "Celine Mac", made once and put on the user's keychain search list; its password (mode 600) |
| `~/Library/LaunchAgents/com.celine.gateway.plist` | the gateway under `caffeinate -d` while the agent's user is logged in |
| `~/.local/share/celine/opencode/opencode` | opencode |
| `~/.config/opencode/opencode.json` | its own keys set on every run, the rest kept: the app's model proxy as provider `celine` (`<app>/llm/v1`, the link key read from `~/.config/celine/link`), Peekaboo and Playwright MCP (the gateway's own copies, Playwright on the same Chrome), every permission allowed, no autoupdate; the app adds the models of `celine` to the same file (`PATCH /global/config`) while no drone works there |
| `~/Library/LaunchAgents/com.celine.opencode.plist` | `opencode serve` on 0.0.0.0:4096, logging to `~/Library/Logs/celine-opencode.log` |
| `~/.local/share/celine/share.js`, `~/Library/LaunchAgents/com.celine.share.plist` | the agent's shares (`shell__share(device="mac")`) on 0.0.0.0:8767 behind the token: `GET /forward?port=` tunnels to a localhost port, `GET /file?path=` serves a file, as shellboxd does |

With the lid shut, a dummy HDMI plug gives the Mac the display its screenshots need.

In the app:

| What | Where |
|---|---|
| `plugin.toml` | the switch and the four servers, `macscreen`, `macshell`, `macbrowser`, `macfiles`, filled from the form: `${URL}` the Address, `${TOKEN}` the Token; `[drones]`, the device `mac` (server/celine/devices.py): `${AGENTS}` and the Token, `${SHARE}` its share server, the link key its model proxy key |
| `settings.toml` | the form: the setup command, Address, Coding agent, Shares, Token, About This Mac and the Mac's facts (Chip, Memory, macOS, Network, User, opencode: its version), Check, Update |
| `gateway.py` | `GET /api/plugins/mac/setup/<token>` (the setup command's script), `GET /api/plugins/mac/setup.sh`, `POST /api/plugins/mac/gateway` (the report), `POST /api/plugins/mac/events` (the agent's scripts), the setup command, Check, Update |
| `package.json`, `package-lock.json` | the MCP servers' versions and sha512 sums; setup.sh installs them with `npm ci`, served at `GET /api/plugins/mac/package-lock.json` |
| `guides/` | each server's guide |

## Run

With the switch on, its form shows the setup command (and the agent gets it from `settings__read`
while the Mac is not connected). On the Mac, log in with the admin account the agent will use,
open Terminal and run:

```sh
curl -fsSL http://<celine>/api/plugins/mac/setup/<token> | bash
```

The command works once, within an hour. Each time you open the form, it shows a new
command. The script
it gets has the app's address (Settings, General, Network) and the link key filled in, and
reads that account's password from the terminal, once for sudo and once for the screen lock. At its
end it posts its report to the app, which fills the form (the Mac's addresses, its facts,
opencode's version and the token) and starts the four servers; the form then shows Connected with the Mac's
facts. While the app's address is localhost the form asks for its network address in
place.

```sh
bash setup.sh            # install or update; ends with the address and the token
bash setup.sh --json     # the same, the report as one JSON line
bash setup.sh --status   # only the report, nothing installed
```

The report names each server's tool count (`null` when it does not answer), the macOS
permissions it lacks and opencode's version (`null` with the error when it does not
answer). A second run updates the gateway's config and restarts it and
Chrome; the token stays. Update on the form does that from the app: it runs the app's
setup on the Mac over the shell server, and has a badge while the Mac's setup is older.

The app reconnects to the four servers by itself while the Mac is away (a reboot waits
for FileVault's unlock and that account's login); the form says why they are down until then.

## From the Mac

`POST /api/plugins/mac/events` with the link key starts a turn of the agent with a message to itself
(`intercom__send` to "me"), in the conversation the request names or the agent's newest chat:

```sh
curl -fsS -H "Authorization: Bearer $(cat ~/.config/celine/link)" -H 'Content-Type: application/json' \
  -d '{"text": "the backup is done", "conversation": "<id>"}' "$(cat ~/.config/celine/app)/api/plugins/mac/events"
```

The agent's models from the Mac's shell go through the model proxy (`<app>/llm/v1` with the link
key, api/llm_proxy.py), never to a model server's LAN address (the guard's escape rule
stops those). Every zsh on the Mac has `$OPENAI_BASE_URL` and `$OPENAI_API_KEY`:

```sh
curl -fsS -H "Authorization: Bearer $OPENAI_API_KEY" "$OPENAI_BASE_URL/models"
```

## Drones

`sub_agents__start(brief, name, device="mac")` starts a drone, a sub-agent on the Mac: opencode
there works on its instructions in a session of its own, on the agent's model through the app's model proxy (its
queue, freeze and usage), with Peekaboo and Playwright MCP, and reports like any
sub-agent (server/celine/sub_agents/drone.py).

## Permissions, once at the Mac

Peekaboo needs Screen Recording and Accessibility (which also covers what it reports as
Event Synthesizing). macOS asks them of the process a LaunchAgent starts, and every
process under it uses that one's grants. setup.sh builds `~/Applications/Celine.app`
(bundle id `com.celine.mac`, rebuilt only when its source changes) and runs the gateway,
opencode and share.js under it, Peekaboo with `--no-remote` so it asks in its own process.
One app gets every grant, and upgrades of node, peekaboo or opencode keep them. Each
LaunchAgent names it in `AssociatedBundleIdentifiers`.

macOS keeps a grant for the app's designated requirement. setup.sh signs Celine.app with
an identity of its own: a self-signed certificate "Celine Mac", made once in
`~/.local/share/celine/signing.keychain-db` (its password in
`~/.config/celine/signing-password`, the keychain on the user's search list so codesign
finds it). The requirement names the app and that certificate, so a rebuilt Celine.app
keeps its grants. When the requirement changes (the ad-hoc builds before, or a new
certificate), setup.sh clears Celine's old grants (`tccutil reset All com.celine.mac`):
they would stay on in System Settings, hold nothing, and stop macOS from asking.

At its end setup.sh walks through each grant that is still off, one at a time: Celine
asks for it first (`Celine --ask screen` or `--ask accessibility`, opened through
LaunchServices so it asks as Celine), which puts Celine into the list, then System
Settings opens at that list; a Finder window shows Celine to drag in should it be
missing. Then it restarts the gateway (a Screen Recording grant holds from the next
start), reports again and prints each grant as on or still off.

Local Network: macOS asks "Allow Celine to find devices on your local network?" the first
time a server under Celine reaches an address on the network, such as opencode reaching
the app's model proxy or a command of the agent's Mac shell calling the app. Press Allow. setup.sh
raises the question while you are at the Mac (Celine opens the app's address once). Denied,
the agent's drones and the Mac shell's calls to the app fail; turn Celine on later under System
Settings, Privacy & Security, Local Network. share.js reports the grant (`/grants`,
`local_network`): a connection to the app that macOS answers with "no route to host" is
counted as denied. Google Chrome asks the same question for itself when one of its pages
opens an address on the network.

Full Disk Access is optional: no dialog asks for it, and setup.sh offers its list last.
Without it the Mac shell and Mac files stay on: a folder like Desktop or Documents waits
until someone presses Allow on the Mac's screen.

Check on the form, its note, and the agent's `settings__read(target="forms.mac")` name each
permission that is off and where to turn Celine on. The app reads the grants when the form
opens, on Check and when the Mac reports. It also reads them when one of the agent's requests looks
at a Mac server while the grants are unknown (no read has answered since the app started)
or one of them blocks that server, at most every 30 s (gateway.py `GRANTS_AGAIN`). It never
reads them on a timer.
