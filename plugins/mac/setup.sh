#!/bin/bash
# Her own computer, macOS on Apple silicon: Peekaboo (screen), Desktop Commander (shell),
# Playwright MCP (browser, on a Chrome of its own) and the MCP filesystem server (files)
# behind one Supergateway with a bearer token, `opencode serve` for her drones (the
# same token as its password) and share.js for her shares (the same token), kept running by LaunchAgents in her login session under ~/Applications/Celine.app, the one app
# that holds macOS's grants for all of them. The Mac never sleeps or locks its screen, and she runs sudo without a password.
# Run it as her; safe to run again.
#
#   curl -fsSL <app>/api/plugins/mac/setup/<token> | bash   the setup line of the app's
#                                                   form: this script, app and key filled in
#   bash setup.sh [--app URL --key KEY] [--json]    install or update, then report
#   bash setup.sh --status                          report only, install nothing
#
# The report is one JSON line: the gateway address, the token, the drones' address, the
# tools that answer, the macOS permissions each still lacks, opencode's version and the
# Mac's facts (user, home, chip, memory in bytes, macOS version, network port). With
# the app's address and its link key it posts that line to the app.
#
# One { } around everything: bash reads the whole script before it runs, so a command
# that reads its input never eats the rest of a piped script.
{
set -euo pipefail

PORT=8765
AGENTS_PORT=4096                 # opencode serve, for her drones
SHARE_PORT=8767                  # share.js, her shares of the Mac's ports and files
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
SHARE=com.celine.share
OC=$LIB/opencode/opencode
CELINE_APP=$HOME/Applications/Celine.app   # every server runs under it: its grants are theirs
RUN=$CELINE_APP/Contents/MacOS/Celine
OC_LOG=$HOME/Library/Logs/celine-opencode.log
BREW=/opt/homebrew/bin/brew      # Apple silicon only; Intel brew lives in /usr/local

APP='' KEY='' JSON='' STATUS=''
while [ $# -gt 0 ]; do
  case $1 in
    --app) APP=${2%/}; shift ;;
    --key) KEY=$2; shift ;;
    --json) JSON=1 ;;
    --status) STATUS=1 ;;
    *) echo "Unknown option: $1. Use --app, --key, --json or --status." >&2; exit 2 ;;
  esac
  shift
done

say() { [ -n "$JSON$STATUS" ] || printf '== %s\n' "$*"; }

# a Mac linked to another app moves to this one: say so and wait for Return first; its
# new token (below) locks the other app out
MOVED=''
if [ -n "$APP" ] && [ -s "$CONF/app" ] && [ "$(cat "$CONF/app")" != "$APP" ]; then
  MOVED=1
  if [ -z "$JSON$STATUS" ]; then
    printf 'This Mac is connected to Celine at %s.\nIf you continue, it connects to %s instead, and the other Celine loses access.\nPress Return to continue, or Ctrl+C to stop. ' \
      "$(cat "$CONF/app")" "$APP"
    read -r _ </dev/tty 2>/dev/null || true
  fi
fi

# One JSON line: gateway address, token, each server's tool count (null: no answer) and
# the permissions it lacks, asked through the gateway so the grants are the gateway's own.
report() {
  local iface ip port
  iface=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}' || true)
  ip=$(ipconfig getifaddr "$iface" 2>/dev/null || true)
  # the hardware port of the default route's interface: Wi-Fi, Ethernet, Thunderbolt Bridge
  port=$(networksetup -listallhardwareports 2>/dev/null |
    awk -v d="Device: $iface" '/^Hardware Port:/{p=substr($0,16)} $0==d{print p}' || true)
  # JavaScript, not shell; AGENTS_PORT is the script's own, passed on unchanged
  # shellcheck disable=SC2016,SC2097,SC2098
  ADDRESS="http://${ip:-127.0.0.1}:$PORT" TOKEN_FILE="$TOKEN" GATEWAY_PORT="$PORT" \
    AGENTS="http://${ip:-127.0.0.1}:$AGENTS_PORT" AGENTS_PORT="$AGENTS_PORT" \
    SHARE="http://${ip:-127.0.0.1}:$SHARE_PORT" \
    MAC_USER="$(id -un)" MAC_HOME="$HOME" MAC_IFACE="$iface" MAC_NET="$port" \
    MAC_CHIP="$(sysctl -n machdep.cpu.brand_string 2>/dev/null || true)" \
    MAC_MEMORY="$(sysctl -n hw.memsize 2>/dev/null || true)" \
    MAC_OS="$(sw_vers -productVersion 2>/dev/null || true)" \
    "$(brew --prefix)/bin/node" --input-type=module -e '
const { ADDRESS, TOKEN_FILE, GATEWAY_PORT, AGENTS, AGENTS_PORT, SHARE } = process.env;
const env = process.env;
const mac = { user: env.MAC_USER, home: env.MAC_HOME, chip: env.MAC_CHIP,
              memory: Number(env.MAC_MEMORY) || null, macos: env.MAC_OS,
              network: env.MAC_NET, interface: env.MAC_IFACE };
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
// share.js: Full Disk Access, which no dialog asks for (null: it does not answer)
let full_disk_access = null;
try {
  const r = await fetch(`http://127.0.0.1:${new URL(SHARE).port}/grants`, { signal: AbortSignal.timeout(10000),
    headers: { Authorization: `Bearer ${token}` } });
  if (r.ok) full_disk_access = (await r.json()).full_disk_access;
} catch {}
console.log(JSON.stringify({ address: ADDRESS, token, token_file: TOKEN_FILE, agents: AGENTS,
                             share: SHARE, servers, opencode, full_disk_access, mac }));
'
}

if [ -n "$STATUS" ]; then
  if [ ! -x "$BREW" ] || [ ! -x "$(dirname "$BREW")/node" ]; then
    echo "Nothing is installed yet. First run the setup command from the Mac's page in Settings." >&2
    exit 1
  fi
  eval "$("$BREW" shellenv)"
  report
  exit
fi

echo "This installs Homebrew, the Xcode command line tools, Node, Peekaboo, Google Chrome, four MCP servers and opencode, and runs them in the background under the app Celine. It also lets $(id -un) use sudo without a password and turns off sleep and the screen lock. Type your Mac password when it asks."
say "Allowing sudo without a password"
sudo -n true 2>/dev/null || sudo -v   # -v asks even under NOPASSWD while %admin needs one
sudoers=$(mktemp)
echo "$(id -un) ALL=(ALL) NOPASSWD: ALL" > "$sudoers"
sudo visudo -cf "$sudoers" >/dev/null
sudo install -m 440 -o root -g wheel "$sudoers" /etc/sudoers.d/celine
rm -f "$sudoers"

say "Turning off sleep and the screen lock"   # the display stays on and the lid closes without sleep; the screen never locks
sudo pmset -a displaysleep 0 disablesleep 1
sysadminctl -screenLock status 2>&1 | grep -q 'is off' || sysadminctl -screenLock off -password - </dev/tty   # piped: the password from the terminal

say "Installing Homebrew"   # its installer also installs the Xcode command line tools when missing
[ -x "$BREW" ] || NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
eval "$("$BREW" shellenv)"

say "Installing Node, Peekaboo and Google Chrome"
brew install node openclaw/tap/peekaboo
brew install --cask google-chrome    # the browser Playwright MCP drives
NODE=$(brew --prefix)/bin/node

say "Installing the MCP servers"
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

say "Installing opencode"   # the release binary, as the box has it
if [ "$("$OC" --version 2>/dev/null)" != "$OPENCODE_VERSION" ]; then
  zip=$LIB/opencode.zip
  curl -fsSLo "$zip" "https://github.com/anomalyco/opencode/releases/download/v$OPENCODE_VERSION/opencode-darwin-arm64.zip"
  echo "$OPENCODE_SHA256  $zip" | shasum -a 256 -c - >/dev/null
  unzip -oq "$zip" opencode -d "$LIB/opencode" && rm "$zip"
fi

say "Building the Celine app"
# macOS keeps a grant (Screen Recording, Accessibility) for the process a LaunchAgent
# starts, and every process that one starts uses it: Peekaboo under the gateway asks with
# the gateway's grants (launchctl procinfo: "responsible pid"). That process is Celine.app,
# never brew's node (ad-hoc signed: a grant holds only its cdhash, gone with each upgrade,
# https://developer.apple.com/forums/thread/697278) and never one of its Cellar paths.
# Ad-hoc signed too, so built only when its source changes: one grant for good.
src=$(mktemp -d)
cat > "$src/launcher.c" <<'EOF'
// Celine.app's program: starts its arguments as its child and waits, passing on TERM, INT
// and HUP (exec would make the child the process macOS asks). --ask first: asks for
// Screen Recording and Accessibility, which puts Celine into both lists of System Settings.
#include <ApplicationServices/ApplicationServices.h>
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <string.h>
#include <sys/wait.h>

extern char **environ;
static pid_t child;

static void pass(int sig) { if (child > 0) kill(child, sig); }

int main(int argc, char **argv) {
  int first = 1;
  if (argc > 1 && strcmp(argv[1], "--ask") == 0) {
    first = 2;
    if (!CGPreflightScreenCaptureAccess()) CGRequestScreenCaptureAccess();
    const void *k[] = {kAXTrustedCheckOptionPrompt}, *v[] = {kCFBooleanTrue};
    CFDictionaryRef o = CFDictionaryCreate(NULL, k, v, 1, &kCFTypeDictionaryKeyCallBacks,
                                           &kCFTypeDictionaryValueCallBacks);
    AXIsProcessTrustedWithOptions(o);   // asks only while it is off
    CFRelease(o);
  }
  if (first >= argc) return 2;
  signal(SIGTERM, pass);
  signal(SIGINT, pass);
  signal(SIGHUP, pass);
  if (posix_spawnp(&child, argv[first], NULL, NULL, argv + first, environ) != 0) return 127;
  int st;
  while (waitpid(child, &st, 0) < 0)
    if (errno != EINTR) return 1;
  return WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st);
}
EOF
cat > "$src/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.celine.mac</string>
  <key>CFBundleName</key><string>Celine</string>
  <key>CFBundleExecutable</key><string>Celine</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
  <key>NSAppleEventsUsageDescription</key><string>Celine uses the apps on this Mac.</string>
</dict>
</plist>
EOF
if ! diff -rq "$src" "$LIB/app" >/dev/null 2>&1 || ! codesign -v "$CELINE_APP" 2>/dev/null; then
  if ! xcode-select -p >/dev/null 2>&1; then
    echo "The Xcode command line tools are missing. Run xcode-select --install, then run this command again." >&2
    exit 1
  fi
  new=$(mktemp -d)/Celine.app   # built beside, so a failed build leaves the working app
  mkdir -p "$new/Contents/MacOS" "$(dirname "$CELINE_APP")"
  cp "$src/Info.plist" "$new/Contents/"
  clang -O2 -Wall -framework ApplicationServices -o "$new/Contents/MacOS/Celine" "$src/launcher.c"
  codesign --force --sign - "$new"
  rm -rf "$CELINE_APP"
  mv "$new" "$CELINE_APP"
  rm -rf "$(dirname "$new")" "$LIB/app"
  cp -R "$src" "$LIB/app"
fi
rm -rf "$src"

say "Creating the token"
mkdir -p "$CONF" && chmod 700 "$CONF"
[ -z "$MOVED" ] || rm -f "$TOKEN"
[ -s "$TOKEN" ] || (umask 077; openssl rand -hex 32 > "$TOKEN")
chmod 600 "$TOKEN"
if [ -n "$APP" ]; then   # her scripts reach the app with these (README, From the Mac)
  echo "$APP" > "$CONF/app"
  (umask 077; echo "$KEY" > "$CONF/link")
fi

say "Setting up Chrome"   # its own profile; its tabs and logins outlive the browser server's sessions
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

say "Setting up the gateway"
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
      "args": ["mcp", "--allow-foreground", "--no-remote"]
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
  <key>AssociatedBundleIdentifiers</key><array><string>com.celine.mac</string></array>
  <key>ProgramArguments</key>
  <array>
    <string>$RUN</string>
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
say "Setting up shares"   # share.js: the app's shares of the Mac's ports and files (server/celine/shares.py)
cat > "$LIB/share.js" <<'EOF'
// The app asks this as it asks shellboxd, with the token as the bearer:
// GET /forward?port=<n> with Upgrade: tcp: 101, then raw bytes both ways to localhost:<n>
// (502 when nothing listens there); GET /file?path=<path from home, or from />: the file,
// or one byte range of it (206, 416 past its end); 404 missing, 400 "folder": true;
// GET /grants: {"full_disk_access": true or false}.
const http = require("http"), net = require("net"), fs = require("fs"), os = require("os");
const path = require("path"), crypto = require("crypto");
const [port, tokenFile] = process.argv.slice(2);
const token = Buffer.from(`Bearer ${fs.readFileSync(tokenFile, "utf8").trim()}`);
const ok = req => {
  const got = Buffer.from(req.headers.authorization || "");
  return got.length === token.length && crypto.timingSafeEqual(got, token);
};
const json = (res, status, body) => {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
};
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://share");
  if (url.pathname === "/health") return json(res, 200, {});
  if (!ok(req)) return json(res, 401, { error: "Wrong token." });
  // Full Disk Access, without a dialog: macOS never asks for it, a read of its own
  // database fails at once without it (it runs under Celine.app, so the grant is Celine's)
  if (req.method === "GET" && url.pathname === "/grants") {
    let full = true;
    try { fs.closeSync(fs.openSync(path.join(os.homedir(), "Library/Application Support/com.apple.TCC/TCC.db"), "r")); }
    catch { full = false; }
    return json(res, 200, { full_disk_access: full });
  }
  if (req.method !== "GET" || url.pathname !== "/file") return json(res, 404, { error: "No such endpoint." });
  const asked = (url.searchParams.get("path") || "").replace(/^~(?=\/|$)/, os.homedir());
  const p = path.resolve(os.homedir(), asked);
  let st;
  try { st = fs.statSync(p); } catch { return json(res, 404, { error: `No file ${p} on the Mac.` }); }
  if (st.isDirectory()) return json(res, 400, { error: `${p} is a folder.`, folder: true });
  if (!st.isFile()) return json(res, 400, { error: `${p} is not a file.` });
  const size = st.size, m = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range || "");
  let first = 0, last = size - 1;
  if (m && (m[1] || m[2])) {
    if (m[1]) { first = +m[1]; if (m[2]) last = Math.min(+m[2], size - 1); }
    else first = Math.max(0, size - +m[2]);
    if (first > last) {
      res.writeHead(416, { "content-range": `bytes */${size}`, "content-length": 0 });
      return res.end();
    }
  }
  const part = Boolean(m && (m[1] || m[2]));
  res.writeHead(part ? 206 : 200, { "content-type": "application/octet-stream",
    "accept-ranges": "bytes", "content-length": last - first + 1,
    ...(part ? { "content-range": `bytes ${first}-${last}/${size}` } : {}) });
  if (size === 0 || req.method === "HEAD") return res.end();
  fs.createReadStream(p, { start: first, end: last }).pipe(res);
});
server.on("upgrade", (req, sock) => {
  const url = new URL(req.url, "http://share"), n = Number(url.searchParams.get("port"));
  const no = (status, error) => {
    const body = JSON.stringify({ error });
    sock.end(`HTTP/1.1 ${status}\r\nContent-Type: application/json\r\n` +
             `Content-Length: ${Buffer.byteLength(body)}\r\nConnection: close\r\n\r\n${body}`);
  };
  if (!ok(req)) return no("401 Unauthorized", "Wrong token.");
  if (url.pathname !== "/forward" || !Number.isInteger(n) || n < 1 || n > 65535)
    return no("400 Bad Request", "port is a number from 1 to 65535.");
  const up = net.connect({ port: n, host: "localhost" });
  up.once("error", () => no("502 Bad Gateway", `Nothing is running on port ${n}.`));
  up.once("connect", () => {
    up.removeAllListeners("error");
    up.on("error", () => sock.destroy());
    sock.on("error", () => up.destroy());
    sock.write("HTTP/1.1 101 Switching Protocols\r\nUpgrade: tcp\r\nConnection: Upgrade\r\n\r\n");
    up.pipe(sock).pipe(up);
  });
});
server.listen(Number(port), "0.0.0.0");
EOF
cat > "$AGENTS/$SHARE.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$SHARE</string>
  <key>AssociatedBundleIdentifiers</key><array><string>com.celine.mac</string></array>
  <key>ProgramArguments</key>
  <array>
    <string>$RUN</string>
    <string>$NODE</string>
    <string>$LIB/share.js</string>
    <string>$SHARE_PORT</string>
    <string>$TOKEN</string>
  </array>
  <key>WorkingDirectory</key><string>$HOME</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
EOF

say "Setting up the coding agent"   # opencode serve on the app's model proxy (the link key), with Peekaboo and Playwright MCP
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
  screen: { type: "local", enabled: true, command: [e.OC_PEEKABOO, "mcp", "--allow-foreground",
    "--no-remote"] },
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
  <key>AssociatedBundleIdentifiers</key><array><string>com.celine.mac</string></array>
  <key>ProgramArguments</key>
  <array>
    <string>$RUN</string>
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
  echo "$(id -un) is not logged in on the Mac's screen. Log in on the Mac itself, then run this command again." >&2
  exit 1
fi
for label in "$CHROME" "$LABEL" "$OPENCODE" "$SHARE"; do   # each restarts with this run's config
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  while launchctl print "gui/$uid/$label" >/dev/null 2>&1; do sleep 1; done
  launchctl bootstrap "gui/$uid" "$AGENTS/$label.plist"
done
for _ in $(seq 30); do curl -sf "http://127.0.0.1:$CDP/json/version" >/dev/null && break; sleep 1; done
for _ in $(seq 30); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 1; done
for _ in $(seq 30); do curl -sf "http://127.0.0.1:$SHARE_PORT/health" >/dev/null && break; sleep 1; done
for _ in $(seq 30); do   # 401 without the password: it answers
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$AGENTS_PORT/global/health")" = 401 ] && break
  sleep 1
done

line=$(report)
if [ -n "$APP" ]; then   # the app saves the address and the token into its "Her computer" form
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
console.log(`\nAddress: ${r.address}\nToken:   ${r.token}\nAgents:  ${r.agents}\nShares:  ${r.share}\n`);
for (const [name, s] of Object.entries(r.servers))
  console.log(`${name.padEnd(8)} ${s.tools === null ? "no answer: " + s.error : s.tools + " tools"}` +
              (s.missing.length ? `, needs ${s.missing.join(" and ")}` : ""));
console.log(`opencode ${r.opencode.version ?? "no answer: " + r.opencode.error}`);
if (r.full_disk_access === false) console.log("files    needs Full Disk Access");'
again="bash setup.sh --status"   # a piped run left no file: the app serves it
[ -z "$APP" ] || again="curl -fsSL $APP/api/plugins/mac/setup.sh | bash -s -- --status"
case $line in *'"missing":["'*|*'"full_disk_access":false'*)   # a grant is off: the panes, one by one
  pane=x-apple.systempreferences:com.apple.preference.security
  # Celine asks for both once, which lists it in both panes. Through LaunchServices, so it
  # asks as itself: started from here, it would ask as this Terminal.
  open -g "$CELINE_APP" --args --ask /usr/bin/true
  open -R "$CELINE_APP"
  open "$pane?Privacy_ScreenCapture"
  cat <<EOF

In the System Settings window, turn on Celine under Screen & System Audio Recording.
If Celine is not in the list, drag it in from the Finder window.
EOF
  printf 'Press Return when Celine is on. '
  read -r _ </dev/tty || true
  open "$pane?Privacy_Accessibility"
  printf 'Now turn on Celine under Accessibility too (drag it in from the Finder window if it is not in the list), then press Return. '
  read -r _ </dev/tty || true
  open "$pane?Privacy_AllFiles"
  printf 'Now turn on Celine under Full Disk Access too (drag it in from the Finder window if it is not in the list), then press Return. '
  read -r _ </dev/tty || true
esac
echo "Done. To check the Mac later, run: $again"
}
