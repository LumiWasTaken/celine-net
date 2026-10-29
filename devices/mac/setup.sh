#!/bin/bash
# Her own computer, macOS on Apple silicon: Peekaboo (screen), Desktop Commander (shell,
# files) and Playwright MCP (browser) behind one Supergateway with a bearer token, kept
# running by a LaunchAgent in her login session. Run it as her; safe to run again.
#
#   bash setup.sh [--app URL] [--json]    install or update, then report
#   bash setup.sh --status                report only, install nothing
#
# The report is one JSON line: the gateway address, the token, the tools that answer and
# the macOS permissions each still lacks. --app posts that line to the app.
set -euo pipefail

PORT=8765
CONF=$HOME/.config/celine
LIB=$HOME/.local/share/celine
TOKEN=$CONF/token
LABEL=com.celine.gateway
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
LOG=$HOME/Library/Logs/celine-gateway.log   # crashes only: info would log every message, screenshots too
BREW=/opt/homebrew/bin/brew      # Apple silicon only; Intel brew lives in /usr/local

APP='' JSON='' STATUS=''
while [ $# -gt 0 ]; do
  case $1 in
    --app) APP=${2%/}; shift ;;
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
    "$(brew --prefix)/bin/node" --input-type=module -e '
const { ADDRESS, TOKEN_FILE, GATEWAY_PORT } = process.env;
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
};
console.log(JSON.stringify({ address: ADDRESS, token, token_file: TOKEN_FILE, servers }));
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

echo "This installs Homebrew, the Xcode command line tools, Node, tmux, ripgrep, Peekaboo, Google Chrome and three MCP servers, and runs them as a background service."
say homebrew   # its installer also installs the Xcode command line tools when missing
if [ ! -x "$BREW" ]; then
  # The installer asks nothing and needs sudo unlocked; the command line tools download
  # can outlast sudo's five minutes, so the loop keeps it unlocked until the installer ends.
  sudo -v
  while kill -0 $$ 2>/dev/null && sudo -n -v 2>/dev/null; do sleep 50; done &
  keep=$!
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  kill "$keep" 2>/dev/null || true
fi
eval "$("$BREW" shellenv)"
export HOMEBREW_NO_AUTO_UPDATE=1

say packages
brew install --quiet node tmux ripgrep openclaw/tap/peekaboo
# One browser for both: Playwright MCP drives Google Chrome, and Desktop Commander renders
# PDFs with it instead of downloading a Chrome of its own.
brew install --quiet --cask google-chrome
brew pin node peekaboo    # macOS grants bind to these two binaries; an upgrade means granting again
brew cleanup --prune=all --quiet          # the downloads, kept by default
NODE=$(brew --prefix)/bin/node

say servers
mkdir -p "$LIB" "$(dirname "$LOG")" "$(dirname "$PLIST")"
cd "$LIB"
[ -f package.json ] || echo '{ "private": true }' > package.json
# No install scripts: Desktop Commander's would report the install; ripgrep comes from brew.
npm install --save-exact --ignore-scripts --no-audit --no-fund \
  supergateway@4.2.1 @playwright/mcp@0.0.83 @wonderwhy-er/desktop-commander@0.3.1
npm cache clean --force 2>/dev/null   # no second copy of what was just installed

say token
mkdir -p "$CONF" && chmod 700 "$CONF"
[ -s "$TOKEN" ] || (umask 077; openssl rand -hex 32 > "$TOKEN")
chmod 600 "$TOKEN"

say gateway
cat > "$CONF/gateway.json" <<EOF
{
  "port": $PORT,
  "outputTransport": "streamableHttp",
  "stateful": true,
  // One Peekaboo and one browser profile at a time: a client gone without closing its
  // session frees them within a minute. The app lists tools every 15 seconds.
  "sessionTimeout": 60000,
  "mcpServers": {
    "screen": { "command": "$(brew --prefix)/bin/peekaboo", "args": ["mcp"] },
    "shell": {
      "command": "$NODE",
      "args": ["$LIB/node_modules/@wonderwhy-er/desktop-commander/dist/index.js"],
      "env": { "DESKTOP_COMMANDER_DISABLE_TELEMETRY": "1" }
    },
    "browser": {
      "command": "$NODE",
      "args": ["$LIB/node_modules/@playwright/mcp/cli.js", "--browser", "chrome"]
    }
  }
}
EOF
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$NODE</string>
    <string>$LIB/node_modules/supergateway/dist/index.js</string>
    <string>--config</string><string>$CONF/gateway.json</string>
    <string>--apiKeyFile</string><string>$TOKEN</string>
    <string>--healthEndpoint</string><string>/health</string>
    <string>--logLevel</string><string>none</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$(brew --prefix)/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>WorkingDirectory</key><string>$HOME</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
uid=$(id -u)
if launchctl print "gui/$uid" >/dev/null 2>&1; then
  launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true   # restart with this run's config
  while launchctl print "gui/$uid/$LABEL" >/dev/null 2>&1; do sleep 1; done
  launchctl bootstrap "gui/$uid" "$PLIST"
  for _ in $(seq 30); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 1; done
else
  echo "No desktop session for $(id -un): log in at the Mac, then run this again." >&2
  exit 1
fi

line=$(report)
if [ -n "$APP" ]; then   # the route comes with the app's "Her computer" card
  curl -fsS -m 30 -X POST -H 'Content-Type: application/json' --data "$line" \
    "$APP/api/plugins/mac/gateway" >/dev/null || echo "Could not reach $APP: paste the line below instead." >&2
fi
if [ -n "$JSON" ]; then
  echo "$line"
  exit
fi
# shellcheck disable=SC2016  # JavaScript, not shell
echo "$line" | "$NODE" -e '
const r = JSON.parse(require("fs").readFileSync(0, "utf8"));
console.log(`\nAddress: ${r.address}\nToken:   ${r.token}\n`);
for (const [name, s] of Object.entries(r.servers))
  console.log(`${name.padEnd(8)} ${s.tools === null ? "no answer: " + s.error : s.tools + " tools"}` +
              (s.missing.length ? `, needs ${s.missing.join(" and ")}` : ""));'
cat <<EOF

Grant once in System Settings, Privacy & Security, under Screen & System Audio Recording
and under Accessibility (the + button, then Cmd+Shift+G for the path):
  $(realpath "$NODE")
  $(realpath "$(brew --prefix)/bin/peekaboo")
Then: bash setup.sh --status
EOF
