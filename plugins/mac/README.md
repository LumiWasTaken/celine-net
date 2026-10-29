# Her own computer: a Mac

A base plugin: the switch "Own computer" on the "Her computer" card (Setup, and Settings,
Features), its form a Settings page. `setup.sh` makes a Mac (Apple silicon, macOS 15 or
later) a computer she uses from the app. It installs with Homebrew and npm only (no Apple
Account):

| What | For |
|---|---|
| Xcode command line tools, Homebrew | the installs (Homebrew's installer brings the tools) |
| node | the servers |
| Google Chrome (brew cask) | the browser Playwright MCP drives |
| [Peekaboo](https://github.com/openclaw/Peekaboo) (brew) | `screen`: see, click, type, menus, windows |
| [Desktop Commander](https://github.com/wonderwhy-er/DesktopCommanderMCP) 0.3.1 | `shell`: commands, processes, files |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) 0.0.83 | `browser` |
| [Supergateway](https://github.com/supercorp-ai/supergateway) 4.2.1 | the three at `http://<mac>:8765/{screen,shell,browser}/mcp` behind a bearer token |

| Where | What |
|---|---|
| `~/.config/celine/token` | the token (mode 600), made once |
| `~/.config/celine/gateway.json` | the gateway's servers, written on every run |
| `~/.local/share/celine` | the npm packages |
| `~/Library/LaunchAgents/com.celine.gateway.plist` | keeps the gateway running while she is logged in |
| `~/Library/Logs/celine-gateway.log` | the gateway's log |

In the app:

| What | Where |
|---|---|
| `plugin.toml` | the switch and the three servers, `macscreen`, `macshell`, `macbrowser`, filled from the form: `${URL}` the Address, `${TOKEN}` the Token |
| `settings.toml` | the form: the command, Address, Token, Check |
| `gateway.py` | `GET /api/plugins/mac/setup.sh`, `POST /api/plugins/mac/gateway` (the report, with the link key), the command, Check |
| `guides/` | each server's guide |

## Run

With the switch on, the card shows the command, with the app's address and the link key
in it. Logged in at the Mac as her (an admin), in Terminal:

```sh
curl -fsSLo setup.sh http://<celine>/api/plugins/mac/setup.sh && bash setup.sh --app http://<celine> --key <key>
```

Not `curl ... | bash`: the script reads the terminal for her password, and Homebrew's
installer reads it too. At its end it posts its report to the app, which saves the address
and the token into the form and starts the three servers.

```sh
bash setup.sh            # install or update; ends with the address and the token
bash setup.sh --json     # the same, the report as one JSON line
bash setup.sh --status   # only the report, nothing installed
```

On a fresh Mac it asks for her password once (`sudo -v`) before Homebrew's installer, and
keeps sudo unlocked until the installer ends. The report names each server's tool count
(`null` when it does not answer) and the macOS permissions it lacks. A second run updates
the gateway's config and restarts it; the token stays.

## Permissions, once at the Mac

Peekaboo needs Screen Recording and Accessibility. It runs as a child of the gateway's
node, so both binaries setup.sh prints at the end go into System Settings, Privacy &
Security, with the + button and Cmd+Shift+G for the path:

- **Screen & System Audio Recording**: node and peekaboo.
- **Accessibility**: node and peekaboo (this also covers the synthetic clicks and keys
  Peekaboo reports as Event Synthesizing).

Check on the card lists each permission the screen server still lacks. An upgrade of node
or peekaboo changes the binaries, and macOS asks for the permissions again.
