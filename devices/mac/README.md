# Her own computer: a Mac

`setup.sh` makes a Mac (Apple silicon, macOS 15 or later) a computer she uses from the
app. It installs with Homebrew and npm only (no Apple Account):

| What | For |
|---|---|
| Xcode command line tools, Homebrew | the installs (Homebrew's installer brings the tools) |
| node, tmux, ripgrep | the servers; terminals; Desktop Commander's search |
| Google Chrome (brew cask) | the one browser: Playwright MCP drives it, Desktop Commander renders PDFs with it |
| [Peekaboo](https://github.com/openclaw/Peekaboo) (brew, pinned) | `screen`: see, click, type, menus, windows |
| [Desktop Commander](https://github.com/wonderwhy-er/DesktopCommanderMCP) 0.3.1 | `shell`: commands, processes, files (telemetry off) |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) 0.0.83 | `browser` |
| [Supergateway](https://github.com/supercorp-ai/supergateway) 4.2.1 | the three at `http://<mac>:8765/{screen,shell,browser}/mcp` behind a bearer token |

| Where | What |
|---|---|
| `~/.config/celine/token` | the token (mode 600), made once |
| `~/.config/celine/gateway.json` | the gateway's servers, written on every run |
| `~/.local/share/celine` | the npm packages |
| `~/Library/LaunchAgents/com.celine.gateway.plist` | keeps the gateway running while she is logged in |
| `~/Library/Logs/celine-gateway.log` | the gateway's crashes |

## Run

Logged in at the Mac as her (an admin), in Terminal. The app serves the script; download
it, then run it from the file:

```sh
curl -fsSLo setup.sh http://<celine>:8766/devices/mac/setup.sh
bash setup.sh --app http://<celine>:8766   # install or update; posts the report to the app
```

Not `curl ... | bash`: the script reads the terminal for her password, and Homebrew's
installer reads it too.

```sh
bash setup.sh            # install or update; ends with the address and the token
bash setup.sh --json     # the same, the report as one JSON line
bash setup.sh --status   # only the report, nothing installed
```

On a fresh Mac it asks for her password once (`sudo -v`) before Homebrew's installer, and
keeps sudo unlocked until the installer ends. The report names each server's tool count
(`null` when it does not answer) and the macOS permissions it lacks. A second run updates
the gateway's config and restarts it; the token stays.

In the app this folder goes to `<data>/plugins/mac/`: the Address field is the address
printed, the token its secret.

## Permissions, once at the Mac

Peekaboo needs Screen Recording and Accessibility. It runs as a child of the gateway's
node, so both binaries setup.sh prints at the end go into System Settings, Privacy &
Security, with the + button and Cmd+Shift+G for the path:

- **Screen & System Audio Recording**: node and peekaboo.
- **Accessibility**: node and peekaboo (this also covers the synthetic clicks and keys
  Peekaboo reports as Event Synthesizing).

Then `bash setup.sh --status` shows `"missing": []` for `screen`. node and peekaboo are
pinned in Homebrew: an upgrade changes the binaries and the permissions are asked again.
