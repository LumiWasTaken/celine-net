#!/bin/bash
# Her own computer, macOS on Apple silicon: Peekaboo (screen), Desktop Commander (shell),
# Playwright MCP (browser, on a Chrome of its own) and the MCP filesystem server (files)
# behind one Supergateway with a bearer token, and `opencode serve` for her drones (the
# same token as its password), kept running by LaunchAgents in her login session. The Mac never sleeps or locks its screen, and she runs sudo without a password.
# Run it as her; safe to run again.
#
#   bash setup.sh [--app URL --key KEY] [--json]    install or update, then report
#   bash setup.sh --status                          report only, install nothing
#
# The report is one JSON line: the gateway address, the token, the drones' address, the
# tools that answer, the macOS permissions each still lacks and opencode's version. --app posts that line to the app with the key
# its "Her computer" card shows in the command.
set -euo pipefail

PORT=8765
AGENTS_PORT=4096                 # opencode serve, for her drones
OPENCODE_VERSION=1.19.4          # the box's (shellbox/Dockerfile), pinned by its sha256
OPENCODE_SHA256=c400a2f8c8ab7e99478a7f4dd1a466b57bf33888e520d85c55780a5464c4b689
CDP=9222                         # Chrome's DevTools port, on 127.0.0.1 only
CONF=$HOME/.config/celine
LIB=$HOME/.local/share/celine
TOKEN=$CONF/token
AGENTS=$HOME/Library/LaunchAgents
LABEL=com.celine.gateway
CHROME=com.celine.chrome
OPENCODE=com.celine.opencode
OC=$LIB/opencode/opencode
OC_LOG=$HOME/Library/Logs/celine-opencode.log
BREW=/opt/homebrew/bin/brew      # Apple silicon only; Intel brew lives in /usr/local

APP='' KEY='' JSON='' STATUS=''
while [ $# -gt 0 ]; do
  case $1 in
    --app) APP=${2%/}; shift ;;
    --key) KEY=$2; shift ;;
    --json) JSON=1 ;;
    --status) STATUS=1 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { [ -n "$JSON$STATUS" ] || printf '== %s\n' "$*"; }

# One JSON line: gateway address, token, each server's tool count (null: no answer) and
# the permissions it lacks, asked through the gateway so the grants are the gateway's own.
report() {
  local ip
  ip=$(ipconfig getifaddr "$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')" 2>/dev/null || true)
  # shellcheck disable=SC2016  # JavaScript, not shell
  ADDRESS="http://${ip:-127.0.0.1}:$PORT" TOKEN_FILE="$TOKEN" GATEWAY_PORT="$PORT" \
    AGENTS="http://${ip:-127.0.0.1}:$AGENTS_PORT" AGENTS_PORT="$AGENTS_PORT" \
    "$(brew --prefix)/bin/node" --input-type=module -e '
const { ADDRESS, TOKEN_FILE, GATEWAY_PORT, AGENTS, AGENTS_PORT } = process.env;
const fs = await import("node:fs");
const token = fs.existsSync(TOKEN_FILE) ? fs.readFileSync(TOKEN_FILE, "utf8").trim() : null;
const base = `http://127.0.0.1:${GATEWAY_PORT}`;
const head = { Authorization: `Bearer ${token}`, "Content-Type": "application/json",
               Accept: "application/json, text/event-stream" };

async function rpc(path, session, id, method, params = {}) {
  const r = await fetch(base + path, { method: "POST", signal: AbortSignal.timeout(60000),
    headers: session ? { ...head, "Mcp-Session-Id": session } : head,
    body: JSON.stringify(id === null ? { jsonrpc: "2.0", method, params }
                                     : { jsonrpc: "2.0", id, method, params }) });
  if (!r.ok && r.status !== 202) throw new Error(`${method}: HTTP ${r.status}`);
  const text = await r.text();
  const data = text.split("\n").filter(l => l.startsWith("data:")).map(l => l.slice(5)).pop() ?? text;
  return { session: r.headers.get("mcp-session-id") ?? session, body: data ? JSON.parse(data) : null };
}

async function server(name, ask) {
  const path = `/${name}/mcp`;
  let s;
  try {
    const init = await rpc(path, null, 1, "initialize", { protocolVersion: "2025-06-18",
      capabilities: {}, clientInfo: { name: "celine-setup", version: "1" } });
    s = init.session;
    await rpc(path, s, null, "notifications/initialized");
    const list = await rpc(path, s, 2, "tools/list");
    const out = { tools: list.body.result.tools.length, missing: [] };
    if (ask) out.missing = await ask((tool, args) => rpc(path, s, 3, "tools/call", { name: tool, arguments: args }));
    return out;
  } catch (e) {
    return { tools: null, missing: [], error: String(e.message ?? e) };
  } finally {
    if (s) await fetch(base + path, { method: "DELETE", headers: { ...head, "Mcp-Session-Id": s } }).catch(() => {});
  }
}

// Peekaboo`s permissions tool: _meta has one boolean per grant.
async function screenGrants(call) {
  const meta = (await call("permissions", {})).body.result._meta;
  return Object.entries({ screen_recording: "Screen Recording", accessibility: "Accessibility",
                          event_synthesizing: "Event Synthesizing" })
    .filter(([k]) => meta[k] === false).map(([, name]) => name);
}

const servers = {
  screen: await server("screen", screenGrants),
  shell: await server("shell"),
  browser: await server("browser"),
  files: await server("files"),
};
// opencode serve: its version, or why it does not answer
let opencode;
try {
  const r = await fetch(`http://127.0.0.1:${AGENTS_PORT}/global/health`, { signal: AbortSignal.timeout(10000),
    headers: { Authorization: "Basic " + Buffer.from(`opencode:${token}`).toString("base64") } });
  opencode = r.ok ? { version: (await r.json()).version } : { version: null, error: `HTTP ${r.status}` };
} catch (e) {
  opencode = { version: null, error: String(e.message ?? e) };
}
console.log(JSON.stringify({ address: ADDRESS, token, token_file: TOKEN_FILE, agents: AGENTS,
                             servers, opencode }));
'
}

if [ -n "$STATUS" ]; then
  if [ ! -x "$BREW" ] || [ ! -x "$(dirname "$BREW")/node" ]; then
    echo "Nothing is installed yet: run bash setup.sh first." >&2
    exit 1
  fi
  eval "$("$BREW" shellenv)"
  report
  exit
fi

echo "This installs Homebrew, the Xcode command line tools, Node, Peekaboo, Google Chrome, four MCP servers and opencode and runs them as background services, lets $(id -un) run sudo without a password, and turns off sleep and the screen lock."
say sudo
sudo -n true 2>/dev/null || sudo -v   # -v asks even under NOPASSWD while %admin needs one
sudoers=$(mktemp)
echo "$(id -un) ALL=(ALL) NOPASSWD: ALL" > "$sudoers"
sudo visudo -cf "$sudoers" >/dev/null
sudo install -m 440 -o root -g wheel "$sudoers" /etc/sudoers.d/celine
rm -f "$sudoers"

say power   # the display stays on and the lid closes without sleep; the screen never locks
sudo pmset -a displaysleep 0 disablesleep 1
sysadminctl -screenLock status 2>&1 | grep -q 'is off' || sysadminctl -screenLock off -password -

say homebrew   # its installer also installs the Xcode command line tools when missing
[ -x "$BREW" ] || NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
eval "$("$BREW" shellenv)"

say packages
brew install node openclaw/tap/peekaboo
brew install --cask google-chrome    # the browser Playwright MCP drives
NODE=$(brew --prefix)/bin/node

say servers
mkdir -p "$LIB" "$AGENTS"
cd "$LIB"
[ -f package.json ] || echo '{ "private": true }' > package.json
npm install supergateway @playwright/mcp @wonderwhy-er/desktop-commander \
  @modelcontextprotocol/server-filesystem
# Desktop Commander runs every command and writes files of any length; its other
# settings stay as they are
# shellcheck disable=SC2016  # JavaScript, not shell
"$NODE" -e '
const fs = require("fs"), file = process.argv[1];
const c = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {};
fs.mkdirSync(require("path").dirname(file), { recursive: true });
fs.writeFileSync(file, JSON.stringify({ ...c, blockedCommands: [], fileWriteLineLimit: 100000 }, null, 2));
' "$HOME/.claude-server-commander/config.json"

say opencode   # the release binary, as the box has it
if [ "$("$OC" --version 2>/dev/null)" != "$OPENCODE_VERSION" ]; then
  zip=$LIB/opencode.zip
  curl -fsSLo "$zip" "https://github.com/anomalyco/opencode/releases/download/v$OPENCODE_VERSION/opencode-darwin-arm64.zip"
  echo "$OPENCODE_SHA256  $zip" | shasum -a 256 -c - >/dev/null
  unzip -oq "$zip" opencode -d "$LIB/opencode" && rm "$zip"
fi

say token
mkdir -p "$CONF" && chmod 700 "$CONF"
[ -s "$TOKEN" ] || (umask 077; openssl rand -hex 32 > "$TOKEN")
chmod 600 "$TOKEN"
if [ -n "$APP" ]; then   # her scripts reach the app with these (README, From the Mac)
  echo "$APP" > "$CONF/app"
  (umask 077; echo "$KEY" > "$CONF/link")
fi

say chrome   # its own profile; its tabs and logins outlive the browser server's sessions
cat > "$AGENTS/$CHROME.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$CHROME</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Applications/Google Chrome.app/Contents/MacOS/Google Chrome</string>
    <string>--remote-debugging-port=$CDP</string>
    <string>--user-data-dir=$LIB/chrome</string>
    <string>--no-first-run</string>
    <string>--no-default-browser-check</string>
  </array>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
EOF

say gateway
cat > "$CONF/gateway.json" <<EOF
{
  "port": $PORT,
  "outputTransport": "streamableHttp",
  "stateful": true,
  "logLevel": "none",
  // A client gone without closing its session frees its servers within a minute. The
  // app lists tools every 15 seconds.
  "sessionTimeout": 60000,
  "mcpServers": {
    "screen": {
      "command": "$(brew --prefix)/bin/peekaboo",
      "args": ["mcp", "--allow-foreground"]
    },
    "shell": {
      "command": "$NODE",
      "args": ["$LIB/node_modules/@wonderwhy-er/desktop-commander/dist/index.js"]
    },
    "browser": {
      "command": "$NODE",
      "args": ["$LIB/node_modules/@playwright/mcp/cli.js",
               "--cdp-endpoint", "http://127.0.0.1:$CDP"]
    },
    "files": {
      "command": "$NODE",
      "args": ["$LIB/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js", "/"]
    }
  }
}
EOF
cat > "$AGENTS/$LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-d</string>
    <string>$NODE</string>
    <string>$LIB/node_modules/supergateway/dist/index.js</string>
    <string>--config</string><string>$CONF/gateway.json</string>
    <string>--apiKeyFile</string><string>$TOKEN</string>
    <string>--healthEndpoint</string><string>/health</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$(brew --prefix)/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>WorkingDirectory</key><string>$HOME</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
EOF
say drones   # opencode serve on the app's model proxy (the link key), with Peekaboo and Playwright MCP
mkdir -p "$HOME/.config/opencode" "$(dirname "$OC_LOG")"
# only this script's keys are set, the rest stays: the app's models of provider celine
# land in this same file (PATCH /global/config edits opencode.json when it exists)
OC_APP="$APP" OC_NODE="$NODE" OC_LIB="$LIB" OC_CDP="$CDP" OC_PEEKABOO="$(brew --prefix)/bin/peekaboo" \
"$NODE" -e '
const e = process.env, fs = require("fs"), file = process.argv[1];
const c = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {};
c.$schema = "https://opencode.ai/config.json";
c.permission = "allow";
c.autoupdate = false;
c.mcp = { ...c.mcp,
  screen: { type: "local", enabled: true, command: [e.OC_PEEKABOO, "mcp", "--allow-foreground"] },
  browser: { type: "local", enabled: true, command: [e.OC_NODE,
    `${e.OC_LIB}/node_modules/@playwright/mcp/cli.js`, "--cdp-endpoint", `http://127.0.0.1:${e.OC_CDP}`] } };
if (e.OC_APP) c.provider = { ...c.provider, celine: { ...c.provider?.celine,
  npm: "@ai-sdk/openai-compatible", name: "Celine",
  options: { baseURL: `${e.OC_APP}/llm/v1`, apiKey: "{file:~/.config/celine/link}" } } };
fs.writeFileSync(file, JSON.stringify(c, null, 1) + "\n");
' "$HOME/.config/opencode/opencode.json"
cat > "$AGENTS/$OPENCODE.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$OPENCODE</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>OPENCODE_SERVER_PASSWORD=\$(cat "$TOKEN") exec "$OC" serve --hostname 0.0.0.0 --port $AGENTS_PORT</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$(brew --prefix)/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>WorkingDirectory</key><string>$HOME</string>
  <key>StandardOutPath</key><string>$OC_LOG</string>
  <key>StandardErrorPath</key><string>$OC_LOG</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
EOF

uid=$(id -u)
if ! launchctl print "gui/$uid" >/dev/null 2>&1; then
  echo "No desktop session for $(id -un): log in at the Mac, then run this again." >&2
  exit 1
fi
for label in "$CHROME" "$LABEL" "$OPENCODE"; do   # each restarts with this run's config
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  while launchctl print "gui/$uid/$label" >/dev/null 2>&1; do sleep 1; done
  launchctl bootstrap "gui/$uid" "$AGENTS/$label.plist"
done
for _ in $(seq 30); do curl -sf "http://127.0.0.1:$CDP/json/version" >/dev/null && break; sleep 1; done
for _ in $(seq 30); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 1; done
for _ in $(seq 30); do   # 401 without the password: it answers
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$AGENTS_PORT/global/health")" = 401 ] && break
  sleep 1
done

line=$(report)
if [ -n "$APP" ]; then   # the app saves the address and the token into its "Her computer" card
  curl -fsS -X POST -H 'Content-Type: application/json' -H "Authorization: Bearer $KEY" \
    --data "$line" "$APP/api/plugins/mac/gateway" >/dev/null
fi
if [ -n "$JSON" ]; then
  echo "$line"
  exit
fi
# shellcheck disable=SC2016  # JavaScript, not shell
echo "$line" | "$NODE" -e '
const r = JSON.parse(require("fs").readFileSync(0, "utf8"));
console.log(`\nAddress: ${r.address}\nToken:   ${r.token}\nAgents:  ${r.agents}\n`);
for (const [name, s] of Object.entries(r.servers))
  console.log(`${name.padEnd(8)} ${s.tools === null ? "no answer: " + s.error : s.tools + " tools"}` +
              (s.missing.length ? `, needs ${s.missing.join(" and ")}` : ""));
console.log(`opencode ${r.opencode.version ?? "no answer: " + r.opencode.error}`);'
cat <<EOF

Grant once in System Settings, Privacy & Security, under Screen & System Audio Recording
and under Accessibility (the + button, then Cmd+Shift+G for the path):
  $(realpath "$NODE")
  $(realpath "$(brew --prefix)/bin/peekaboo")
  $OC
Then: bash setup.sh --status
EOF
