"""shellboxd: Celine's terminals in its shellbox. Standard library only, plus tmux.

The agent's terminals are tmux sessions on a private socket (`tmux -L shellbox`), one session per
terminal, each running its login shell as a login shell. tmux owns them: it draws the
screen, keeps the scrollback and answers what programs ask a terminal. This daemon runs
as root (nothing it does stops it) and starts everything of its own as its user, celine;
what it writes in its home, it writes as it. It types into its terminals, reads them and returns
facts; the app (server/celine/skills/shell.py) words them.

    POST /run   {"terminal", "command", "timeout"?, "chars"?, "owner"?}
                -> lines of JSON: {"started": cmd id} once the command is typed,
                   {"progress": line} when the command's last output line
                   changed (at most once a second), then exactly one result; a run's
                   result (also from /stop and /read) carries "outside" and
                   "outside_from", see outside()
    POST /wait  {"terminal", "timeout"?, "chars"?} -> as /run, for the run already going
                   in that terminal: watched until it ends or the timeout; its result
                   at once when it ended already
    POST /type  {"terminal", "text"?, "keys"?, "enter"?, "owner"?} -> the screen
    POST /stop  {"terminal", "chars"?}           -> Ctrl-C; the run it ended, or the screen;
                   "survived": the programs still in front after it; the next /stop
                   sends those SIGTERM, then SIGKILL ("killed": {"signal", "names"})
    POST /read  {"terminal"?, "from"?, "chars"?} -> the latest run's log from byte `from`
                   on, or the screen (no `from`, or a full-screen program); without
                   terminal the list of its terminals
    POST /control {"terminal", "action"}         -> the user's stop (Ctrl-C), pause,
                   resume or close; {"ok", "action", "terminal", "id", "program", "pid",
                   "screen" (close only)}
    POST /keys  {"terminal", "data"}             -> raw bytes (base64, at most KEYS_MAX)
                   into the user's own terminal; {"ok", "line"}: the command line when
                   Enter went to the shell at its prompt, else null; {"refused"} when
                   the terminal is paused
    GET  /status                                 -> how this start went; also the
                   app's probe of the key
    GET  /view?terminal=main                     -> the terminal's bytes, live, read-only
    GET  /log?id=<terminal id>&from=<offset>     -> the terminal's raw log from that byte
                   on, at most LOG_CHUNK bytes; X-Log-Size says the log's whole size
    PUT  /file?path=<in its home>[&origin=<url>] -> the raw body as its file
                   (Content-Length needed, at most what its disk has left), via a temp
                   name; origin: tagged with it; {"path", "bytes"}
    GET  /file?path=<in its home>               -> its file, streamed; one byte range of it
                   with Range: bytes=a-b (206, 416 past its end); 404 when missing, 400
                   with "folder": true for a folder
    GET  /forward?port=<n>                       -> with Upgrade: tcp, 101 and then the raw
                   bytes both ways to localhost:<n> in its box, until either side ends
                   (a share: server/celine/shares.py); 502 when nothing listens there.
                   The app sends nothing before the 101.
    POST /code/session {"dir", "task", "model", "session"?, "websearch"?} -> the coding
                   agent works on the task in its folder, in a new session or the given
                   one; {"session", "state": "running", "dir", "model", "terminal",
                   "terminal_id"}: the terminal code-<id> shows the session live
                   (or "terminal_error")
    GET  /code/session/<id>                      -> {"session", "agent", "state": running,
                   done or failed, "error"?, "dir", "title", "updated"}
    GET  /code/session/<id>/result               -> the same plus "text" (its last answer),
                   "tool_output" (the tail of what its tools printed), "files" (changed)
    POST /code/session/<id>/abort                -> {"session", "aborted"}
    GET  /code/sessions                          -> [{"session", "title", "dir", "updated"}]
                   The coding agent is one module in shellbox/agents/ (CODING_AGENT names
                   it, default opencode); the box holds its password, the app never does.
    GET  /health                                 -> {"ok": true}, without the key
    Every answer carries X-Shellbox-Reset: when the box was last reset (a share ends
    with it).
    Authorization: Bearer <the app's key> on every other call. The box never holds the
    key, only its sha256 (/etc/shellbox/key.hash): it has sudo, and root reads any
    environment.

A run. /run types ` __shellbox_run <id> '<command>'` into the shell. That function (RUN_SH,
written to /etc/shellbox/run.sh at every start, sourced by /etc/bash.bashrc) writes
CMD/<id>.start (the start time, the command), runs the command in the shell itself (cd,
variables and a venv stay) with its output into CMD/<id>.log only (no tee: a `&` job
cannot hold the terminal), then writes CMD/<id>.exit (the exit status and the duration)
itself, so no prompt hook of its own can keep a run from ending. After Ctrl-C (bash leaves
the function) the D mark's hook writes it. The daemon copies the log into the terminal as
it grows (echo), for the screen and the live view; the run waits for that copy before its
prompt, so the output stands above it: it opens the fifo CMD/<id>.sync first thing, and
the daemon writes a line into it once the copy is whole.
A run ends with the first of these, looked at every TICK seconds (files and /proc only):
  done         CMD/<id>.exit is there
  closed       the shell exited
  not_started  no CMD/<id>.start after START_WAIT seconds: the shell has no run function
  ended        the shell is alone at its own prompt (readline, at_prompt) for SETTLE
               seconds with no CMD/<id>.exit: the run ended, its exit is unknown
               (`exec zsh`, Ctrl-C under a hook that writes no mark)
  waiting      the command is blocked reading the terminal for WAITING_FOR seconds
               (reads_terminal): it asks for input
  running      the timeout is over; the command keeps running
A run is refused (state busy) while anything runs in front of the shell, a command mark
is open and the shell is not alone at its prompt, the terminal is paused, or the shell
waits for the rest of a line. /type and /stop are never refused. A program that reads
Ctrl-C as a key (a REPL, nano) outlives a /stop; the next /stop ends what outlived it,
SIGTERM and after KILL_WAIT seconds SIGKILL, so two /stops always free a terminal (a
paused program never counts as outliving one). Nothing is ever stopped without a /stop
or the user's control, with one exception: at MAX_TERMINALS a new terminal closes the
one of its own that waited at its prompt quietest the longest, IDLE_CLOSE seconds at least
(the note made_room:<name>); when none did, the new one is refused with each open
terminal and what it runs.

Every terminal has a permanent id (6 of [a-z0-9], minted when it opens, never reused) and
a raw log of everything it printed, LOGS/<id>.raw: root's (its terminal may only append
to it), past LOG_ROTATE bytes moved to LOGS/<id>.<from>-<to>.raw.gz, never deleted,
readable after the terminal closed, gone only with the reset. A run's log is gzipped
CMD_GZIP_AFTER seconds after it ended, never deleted. A terminal has an owner: celine,
or the user's short name when the app opens one for the user (`owner` counts only on
the call that opens it). The shell sees them as SHELLBOX_ID, SHELLBOX_TERMINAL and
SHELLBOX_OWNER. The listing reaps terminals whose shell exited (after listing them once).

Pause sends SIGSTOP to the program's group, like Ctrl-Z: the shell says "Stopped" and
shows its prompt; resume types `fg`. A control on one of its terminals leaves a note for
its next call on that name: stopped:<program>:<epoch> (program empty at the prompt),
paused:<program>:<epoch>, resumed:<program>:<epoch>, closed:<epoch>. Controls on the
user's own terminals leave none.

At every start a home that is not its own (a new volume, another uid) is given to it, said
in the container's log; the box runs apt-get update, then ~/boot.sh as it, in the
background (STATUS apt and boot). With SHELLBOX_HOME_GB (the hard cap, default 50; the soft
cap SHELLBOX_HOME_SOFT_GB, default 90% of it), its home is measured every HOME_EVERY seconds
(USAGE for the greeting); its next result after a measure past a cap carries the note
home_over:<used>:<cap>, once per measure, and past the hard cap no upload lands. Nothing
else the agent writes passes through here: the wall for that is the host's
(shellbox/home-disk.sh).
"""
from __future__ import annotations

import base64
import binascii
import fcntl
import glob
import gzip
import hashlib
import hmac
import importlib.util
import io
import itertools
import json
import os
import pty
import pwd
import re
import secrets
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

USER = "celine"                     # its uid is the image's (HER_UID, docker-compose.yml)
HOME = Path(os.path.expanduser(f"~{USER}"))
# the app's key never enters the box, only its sha256, in a file compose mounts read-only
KEY_HASH_FILE = Path("/etc/shellbox/key.hash")
KEY_HASH = KEY_HASH_FILE.read_text().strip() if KEY_HASH_FILE.is_file() else ""
PORT = 8770

COLS = 120                          # its terminal's width: a full screen stays under 2k tokens
ROWS = 40                           # its terminal's height
TICK = 0.2                          # seconds between looks at a run, and what one /proc walk serves
START_WAIT = 5.0                    # no CMD/<id>.start this long after typing: not_started
WAITING_FOR = 1.0                   # a run blocked reading the terminal this long: waiting
ECHO_TICK = 0.05                    # seconds between two copies of a run's log into its terminal
SYNC_WAIT = 2                       # seconds a run waits for that copy before its prompt
PROMPT_WAIT = 10.0                  # a new terminal: seconds for its greeting and first prompt
ECHO_WAIT = 1.0                     # /keys: seconds for the shell to echo the line typed
DRAIN_WAIT = 2.0                    # a rotated log: seconds for the pane's old writer to let go
SETTLE = 1.0                        # /type: the terminal printed nothing this long: the screen
SETTLE_MAX = 15.0                   # and at most this long after typing
STOP_WAIT = 5.0                     # /stop waits this long for the run it stopped to end
KILL_WAIT = 2.0                     # a second /stop: seconds between SIGTERM and SIGKILL
RUN_MAX = 3000                      # seconds one /run waits at most
# the syscalls a program blocks in while it waits for the terminal (/proc/<pid>/syscall)
READS = {"x86_64": {0, 17, 19, 275}, "aarch64": {63, 65, 67, 76}}    # read, pread, readv, splice
POLLS = {"x86_64": {7, 23, 232, 270, 271, 281, 441},                      # poll, select, epoll
         "aarch64": {22, 72, 73, 441}}
HEAD, TAIL = 2000, 6000             # characters of a long output kept from its start and end
PAGE_MAX = 64 * 1024                # characters one /read page carries at most
MAX_TERMINALS = 8                   # about 35 MB each with a full scrollback
IDLE_CLOSE = 600                    # seconds at its prompt, quiet, before a terminal may make room
LOG_CHUNK = 1024 * 1024             # bytes one /log answer carries at most
LOG_ROTATE = 64 * 1024 * 1024       # a raw log past this many bytes is moved aside, gzipped
CMD_GZIP_AFTER = 3600               # seconds after a run ended its log is gzipped
HOUSEKEEP_EVERY = 60                # seconds between two rounds of rotating and gzipping
BOOT_LIMIT = 900                    # seconds for ~/boot.sh
APT_LIMIT = 300                     # seconds for the package lists at start
HOME_EVERY = 600                    # seconds between two measures of its home
VIEW_TIMEOUT = 15                   # seconds a live viewer may stall before it is dropped
PROGRESS_EVERY = 1.0                # seconds between progress lines at most
KEYS_MAX = 4096                     # bytes one /keys call types at most
KEYS_CHUNK = 512                    # bytes per send-keys call
FORWARD_WAIT = 5.0                  # seconds /forward waits to reach its server's port

TMUX = ["tmux", "-L", "shellbox", "-f", "/etc/shellbox/tmux.conf"]
MARKER = Path("/var/lib/shellbox/started")    # outside its home: gone after the reset
LOGS = MARKER.parent / "logs"                  # each terminal's raw output, by id
CMD = MARKER.parent / "cmd"                    # its runs: <id>.start, <id>.log, <id>.exit
RESET = MARKER.parent / "reset"                # when the last reset was (the greeting)
USAGE = MARKER.parent / "home"                 # "<used> <limit>" in bytes (the greeting)
RC = Path("/etc/shellbox/run.sh")              # RUN_SH, sourced by /etc/bash.bashrc
HOME_GB = os.environ.get("SHELLBOX_HOME_GB") or "50"   # its home's hard cap
HOME_SOFT_GB = os.environ.get("SHELLBOX_HOME_SOFT_GB", "")   # warned past; empty: 90%

# what its terminals see, and nothing else (tmux hands the environment of whoever starts
# its server to every terminal). pip and npm settings come from
# /etc/profile.d/shellbox.sh in its login shells.
ENV = {
    "HOME": str(HOME), "USER": USER, "LOGNAME": USER,
    "PATH": f"{HOME}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8", "TZ": os.environ.get("TZ", "UTC"),
}
# its models, through the app's model proxy (server/celine/api/llm_proxy.py): the key is
# the proxy's own, never a provider's, so it may sit in its environment
LLM_URL = os.environ.get("SHELLBOX_LLM_URL", "")
LLM_KEY_FILE = Path("/etc/shellbox/llm.key")
LLM_KEY = LLM_KEY_FILE.read_text().strip() if LLM_KEY_FILE.is_file() else ""
if LLM_URL and LLM_KEY:
    ENV.update(OPENAI_BASE_URL=f"{LLM_URL}/v1", OPENAI_API_KEY=LLM_KEY)
# the coding agent (shellbox/agents/<name>.py, /code/* below): loaded in start()
CODING_AGENT = os.environ.get("CODING_AGENT", "opencode")
AGENTS_DIR = Path("/usr/local/lib/shellbox/agents")
CODE = None                 # the loaded backend module
# ~/boot.sh is no login shell, so it gets those two here
BOOT_ENV = {**ENV, "PIP_BREAK_SYSTEM_PACKAGES": "1", "NPM_CONFIG_PREFIX": f"{HOME}/.local"}
AS_HER = {"user": USER, "group": USER, "extra_groups": []}

# The agent's shell's side of the protocol, in bash. The marks: C before a command runs (PS0), D;<exit>
# before each prompt. After the D mark, a line of spaces and a return: after output
# without a final newline they run over the edge onto a fresh line, at the start of a
# line they stay on it; then that line is cleared, so the prompt always starts at the
# left edge and readline never garbles the line. PS0 clears the line the output starts on
# (it drops the wrap mark tmux keeps on a command line that exactly fills its lines).
RUN_SH = r"""# shellbox: written by shellboxd at every start, sourced by /etc/bash.bashrc.
# __shellbox_run <id> <command>: what shell__run types. It runs the command in this shell,
# its output into {cmd}/<id>.log only (the daemon copies it into the terminal); <id>.start
# holds the start time and the command, <id>.exit the exit status and the seconds it took,
# written right after the command; then it waits (at most {sync}s) on the fifo <id>.sync,
# opened before the command, until the daemon's copy caught up. After Ctrl-C bash leaves
# the function: the D mark's hook writes <id>.exit then and closes the fifo. A ~/.bashrc that sets PROMPT_COMMAND instead of adding to it
# drops the mark; a run puts it back before and after the command, so its own hooks keep
# running after it.
__shellbox_hook() {{
    case " ${{PROMPT_COMMAND[*]}} " in *" __shellbox_mark "*) ;;
        *) PROMPT_COMMAND=(__shellbox_mark "${{PROMPT_COMMAND[@]}}") ;; esac
}}
__shellbox_end() {{
    local us
    if [ -n "$__shellbox_id" ]; then
        us=$(( ${{EPOCHREALTIME/./}} - ${{__shellbox_t0/./}} ))
        printf '%s %d.%06d\n' "$1" $((us / 1000000)) $((us % 1000000)) \
            > "{cmd}/$__shellbox_id.exit"
        __shellbox_id=
    fi
}}
__shellbox_unsync() {{
    [ -n "$__shellbox_sync" ] && exec {{__shellbox_sync}}<&-
    __shellbox_sync=
}}
__shellbox_run() {{
    __shellbox_hook
    __shellbox_id=$1 __shellbox_t0=$EPOCHREALTIME
    [ -p "{cmd}/$1.sync" ] && exec {{__shellbox_sync}}<> "{cmd}/$1.sync"
    printf '%s\n%s\n' "$__shellbox_t0" "$2" > "{cmd}/$1.start"
    history -s -- "$2"
    {{ eval "$2"; }} >> "{cmd}/$1.log" 2>&1
    set -- $? "$1"
    __shellbox_end "$1"
    [ -n "$__shellbox_sync" ] && read -rt {sync} -u "$__shellbox_sync"
    __shellbox_unsync
    __shellbox_hook
    return $1
}}
__shellbox_mark() {{
    local s=$?
    __shellbox_end "$s"
    __shellbox_unsync
    printf '\e]133;D;%s\a%*s\r\e[K' "$s" "$COLUMNS" ''
    return $s
}}
PROMPT_COMMAND=(__shellbox_mark "${{PROMPT_COMMAND[@]}}")
PS0='\e]133;C\a\e[K'
"""

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")
ID = re.compile(r"^[a-z0-9]{6}$")
# the keys the app may send, in tmux's names; any single character stands for itself
NAMED_KEYS = {"Enter", "Tab", "BTab", "Escape", "BSpace", "DC", "IC", "Up", "Down", "Left",
              "Right", "Home", "End", "PPage", "NPage", "Space",
              *(f"F{n}" for n in range(1, 13))}
CTRL_ALT = re.compile(r"^[CM]-[!-~]$")                       # C-c, C-\, M-b
# the shell's invisible marks: C = a command starts, D;n = it ended with exit code n
MARK = re.compile(rb"\x1b\]133;([CD])([^\x07\x1b]*)(?:\x07|\x1b\\)")
# what prints nothing: escape sequences and control characters other than tab, line
# break and return (clean() reads those)
INVISIBLE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b.|"
                       rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
REVERSE = re.compile(r"\x1b\[(?:[0-9;]*;)?7(?:;[0-9;]*)?m")   # reverse video
CONTINUATION = re.compile(r"^> ?$")                          # bash's "> "
LAUNCH = 'unset TMUX TMUX_PANE; export SHELL="$0"; exec -a "-${0##*/}" "$0"'
CLOCK = os.sysconf("SC_CLK_TCK")

# The outside flag on a run's answer (the guard):
# its output is text from the net when the command line reaches the net, or names a file
# tagged with where it came from (user.celine.origin, by net-tag and PUT /file?origin=).
ORIGIN = "user.celine.origin"
NET = {"curl", "wget", "ssh", "scp", "sftp", "nc", "ncat", "netcat", "socat", "telnet", "ftp",
       "http", "https", "xh", "aria2c", "lynx", "w3m", "links", "yt-dlp", "youtube-dl", "npx",
       "dig", "nslookup", "whois"}
# these reach the net with one of their subcommands: `git pull`, `pip install`
NET_SUB = {"git": {"clone", "pull", "fetch", "submodule", "ls-remote"},
           "pip": {"install", "download"}, "pip3": {"install", "download"},
           "pipx": {"install", "run"}, "uv": {"add", "sync", "pip"},
           "npm": {"install", "i", "ci", "add", "update", "exec"},
           "pnpm": {"install", "i", "add", "update", "dlx"},
           "yarn": {"install", "add", "upgrade", "dlx"}, "bun": {"install", "add", "x"},
           "cargo": {"install", "add", "fetch"}, "gem": {"install"}, "go": {"install", "get"}}
# what stands before the program it starts: `sudo curl`, `time git pull`, `xargs wget`
LEAD = {"sudo", "env", "time", "nohup", "exec", "command", "nice", "ionice", "timeout",
        "stdbuf", "xargs", "watch", "then", "do", "else", "!", "{", "if", "while", "until"}
SHELLS = {"sh", "bash", "dash", "zsh"}               # `bash -c "curl x"`: the string counts
LEADING = re.compile(r"^(?:-.*|\w+=.*|\d+[smhd]?)$")  # options, VAR=x, `timeout 5`
URL_IN = re.compile(r"\b(?:https?|ftp|wss?)://[^\s'\"<>|;&()]+", re.I)
OPS = set(";&|()<>")

STATUS: dict = {}                 # how this start went, see start()
TERMS_LOCK = threading.Lock()     # opening and removing terminals
LOGS_LOCK = threading.Lock()      # a raw log's rotation, and reading across it
tmux_gone = False                 # its terminals ended without a reset, see check_server()
housekept = 0.0                   # when housekeep() last ran


class Problem(Exception):
    """A plain sentence, answered as {"error": ...} with this HTTP status."""

    def __init__(self, text: str, status: int = 400, **extra):
        super().__init__(text)
        self.status, self.extra = status, extra


class HungUp(Exception):
    """The app closed the connection; the terminal carries on."""


class Terminal:
    """What the daemon remembers about one terminal. tmux holds everything else."""

    def __init__(self, owner: str = "celine"):
        self.id = new_id()            # permanent, never reused: the app's key for it
        self.owner = owner            # celine, or the user's short name
        self.opened_at = int(time.time())
        self.paused = None            # {"pgid": the stopped program's group} while paused
        self.lock = threading.Lock()  # one delivery at a time: keys, paste, Enter
        self.cmd = None               # its latest run: {"id", "command"}
        self.shell = ""               # the shell's name, to notice an `exec zsh`
        # the marks in the raw log, read up to `pos`
        self.scan_lock = threading.Lock()
        self.pos, self.carry = 0, b""
        self.open = False             # a C mark without its D: a command runs
        self.last_d = None            # the exit code of the latest D mark
        self.survived: set[int] = set()   # pids in front that outlived the last /stop's Ctrl-C

    def scan(self) -> None:
        """Reads the marks its shell wrote since the last scan."""
        with self.scan_lock:
            size = log_size(self.id)
            if size == self.pos:
                return
            buf = self.carry + read_log(self.id, self.pos, size)
            end = 0
            for m in MARK.finditer(buf):
                if m[1] == b"C":
                    self.open = True
                else:
                    code = re.match(rb";(\d+)", m[2])
                    self.open, self.last_d = False, int(code[1]) if code else None
                end = m.end()
            # a mark may be cut in two by a read: keep the unfinished end for next time
            self.carry, self.pos = buf[max(end, len(buf) - 512):], size


terms: dict[str, Terminal] = {}
ids_used: set[str] = set()
pending_notes: dict[str, list[str]] = {}    # by name: what the user did to its terminal


def new_id() -> str:
    """Never one used before, in this start or an earlier one (its files are still
    there), for a terminal or a run."""
    while True:
        new = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(6))
        if new not in ids_used and not any(LOGS.glob(f"{new}.*")) \
                and not any(CMD.glob(f"{new}.*")):
            ids_used.add(new)
            return new


def known(name: str) -> Terminal:
    """The record of a terminal; made on first sight for one this daemon did not open
    (a session it started itself). Never a fresh Terminal for a known name: every
    one made mints an id."""
    t = terms.get(name)
    if t is None:
        t = terms[name] = Terminal()
    return t


# ---- tmux and /proc ------------------------------------------------------------------

def tmux(*args: str, stdin: bytes | None = None) -> str:
    r = subprocess.run([*TMUX, *args], input=stdin, capture_output=True, env=ENV, **AS_HER)
    if r.returncode:
        raise Problem("tmux " + args[0] + ": " + (r.stderr.decode(errors="replace").strip()
                                                  or f"exit {r.returncode}"))
    return r.stdout.decode(errors="replace")


def tmux_ok(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*TMUX, *args], capture_output=True, env=ENV, **AS_HER)


def target(name: str) -> str:
    return f"={name}:"                  # exactly this session, its one pane


def names() -> list[str]:
    r = tmux_ok("list-sessions", "-F", "#{session_name}")
    return sorted(r.stdout.decode().split()) if r.returncode == 0 else []


def live() -> list[str]:
    """The terminals whose shell still runs: what counts against MAX_TERMINALS."""
    r = tmux_ok("list-panes", "-a", "-F", "#{session_name} #{pane_dead}")
    if r.returncode != 0:
        return []
    return sorted(ln.split()[0] for ln in r.stdout.decode().splitlines()
                  if ln.endswith(" 0"))


def exists(name: str) -> bool:
    return tmux_ok("has-session", "-t", target(name)).returncode == 0


def pane(name: str) -> dict:
    keys = ["pane_pid", "pane_dead", "pane_dead_status", "alternate_on", "cursor_x",
            "cursor_y", "pane_pipe", "pane_current_path", "pane_tty"]
    values = tmux("display-message", "-p", "-t", target(name),
                  "\t".join("#{%s}" % k for k in keys)).rstrip("\n").split("\t")
    p = dict(zip(keys, values))
    if not p["pane_pid"].isdigit():             # a session on its way out
        raise Problem(f"No terminal named {name}.", 404)
    return {"pid": int(p["pane_pid"]), "dead": p["pane_dead"] == "1",
            "exit": int(p["pane_dead_status"]) if p["pane_dead_status"].isdigit() else None,
            "fullscreen": p["alternate_on"] == "1", "pipe": p["pane_pipe"] == "1",
            "row": int(p["cursor_y"]), "col": int(p["cursor_x"]), "tty": p["pane_tty"],
            # tmux runs as it, so it may read its processes' directory; root here may not
            "cwd": p["pane_current_path"] or None}


def proc(pid: int) -> dict | None:
    """One process from /proc/<pid>/stat."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(errors="replace")
    except OSError:
        return None
    f = raw[raw.rindex(")") + 2:].split()
    return {"pid": pid, "name": raw[raw.index("(") + 1:raw.rindex(")")], "state": f[0],
            "ppid": int(f[1]), "pgrp": int(f[2]), "tpgid": int(f[5]),
            "start": int(f[19]) / CLOCK}


walked = (0.0, {})                # (when, every process by its parent), see procs()
WALK_LOCK = threading.Lock()


def procs() -> dict[int, list[dict]]:
    """Every process by its parent: one walk of /proc serves every caller for TICK
    seconds, however many runs are being watched."""
    global walked
    with WALK_LOCK:
        if time.monotonic() - walked[0] >= TICK:
            children: dict[int, list[dict]] = {}
            for entry in os.scandir("/proc"):
                if entry.name.isdigit() and (p := proc(int(entry.name))):
                    children.setdefault(p["ppid"], []).append(p)
            walked = (time.monotonic(), children)
        return walked[1]


def foreground(shell_pid: int, pgid: int | None = None) -> tuple[dict | None, list[dict]]:
    """The shell, and the job in front of it: every process of the terminal's foreground
    group (found by group, never by its leader: the leader of `seq | less` is long gone),
    and everything its programs started, in whatever group (apt runs dpkg in a session
    of its own). A shell's children in a group other than its own are its background
    jobs and stay out. In tree order, each process before what it started: `sudo apt-get | tail`
    reads sudo, apt-get, tail. With `pgid`, that group instead: the paused program,
    while the shell holds the terminal. The shell itself is in the job when it is alone."""
    shell = proc(shell_pid)
    if shell is None:
        return None, []
    want = pgid or shell["tpgid"]
    children = procs()
    group = sorted((p for kids in children.values() for p in kids if p["pgrp"] == want),
                   key=lambda p: p["pid"])
    in_group = {p["pid"] for p in group}
    shells = shell_names(shell)
    job: list[dict] = []

    def take(p: dict) -> None:
        job.append(p)
        for c in sorted(children.get(p["pid"], []), key=lambda c: c["pid"]):
            if c["pgrp"] == p["pgrp"] or p["name"] not in shells:
                take(c)
    for p in group:
        if p["ppid"] not in in_group:
            take(p)
    return shell, job


def in_front(t: Terminal, p: dict) -> tuple[dict | None, list[dict], list[dict]]:
    """The shell, the job in front (the shell when it is alone), and that job without
    the shell: what runs (or waits, paused) in front of it."""
    shell, job = foreground(p["pid"], t.paused["pgid"] if t.paused else None)
    return shell, job, [g for g in job if g["pid"] != p["pid"]]


def reads_terminal(job: list[dict]) -> bool:
    """A process of the job is blocked waiting for the terminal, sockets or not: in a
    read on it, in a poll or select on nothing but terminals (Python's REPL polls with a
    timeout), or in a poll without a deadline with the terminal as its input (ssh). A
    sleep, a download or a server polling with a deadline is running. Reading other
    processes' syscalls needs SYS_PTRACE (docker-compose.yml); without it nothing counts
    as waiting."""
    arch = os.uname().machine
    reads, polls = READS.get(arch, set()), POLLS.get(arch, set())
    for g in job:
        try:
            call = Path(f"/proc/{g['pid']}/syscall").read_text().split()
        except OSError:
            continue
        if not call or not call[0].isdigit():
            continue                                   # running, not blocked
        nr = int(call[0])

        def tty(fd: int) -> bool:
            try:
                return os.readlink(f"/proc/{g['pid']}/fd/{fd}").startswith(
                    ("/dev/pts/", "/dev/tty"))
            except OSError:
                return False
        if nr in reads and tty(int(call[1], 16)):
            return True
        if nr in polls:
            fds = watched(g["pid"], arch, nr, call[1:])
            if fds and all(tty(fd) for fd in fds):
                return True
            if tty(0) and no_deadline(arch, nr, call[1:]):
                return True
    return False


# the polls whose file descriptors can be read from the program's memory: where the
# array of struct pollfd and its length sit, or the read fd_set and the count of a select
FD_SETS = {"x86_64": {7: ("poll", 0, 1), 271: ("poll", 0, 1), 23: ("select", 1, 0),
                      270: ("select", 1, 0)},
           "aarch64": {73: ("poll", 0, 1), 72: ("select", 1, 0)}}


def watched(pid: int, arch: str, nr: int, args: list[str]) -> list[int]:
    """The file descriptors a blocked poll or select waits on, empty when unknown."""
    kind, at, count = FD_SETS.get(arch, {}).get(nr, (None, 0, 0))
    try:
        where, n = int(args[at], 16), min(int(args[count], 16), 64)
        with open(f"/proc/{pid}/mem", "rb") as mem:
            mem.seek(where)
            if kind == "poll":
                data = mem.read(8 * n)
                return [struct.unpack_from("i", data, 8 * i)[0] for i in range(n)]
            if kind == "select" and where:
                data = mem.read((n + 7) // 8)
                return [i for i in range(n) if data[i // 8] >> (i % 8) & 1]
    except (OSError, ValueError, IndexError, struct.error):
        pass
    return []


# where a poll's deadline sits in its arguments: an int (-1 means none) or a pointer
# to a timespec (0 means none). x86_64: poll, select, epoll_wait, pselect6, ppoll,
# epoll_pwait, epoll_pwait2; aarch64: epoll_pwait, pselect6, ppoll, epoll_pwait2.
DEADLINES = {"x86_64": {7: (2, "int"), 23: (4, "ptr"), 232: (3, "int"), 270: (4, "ptr"),
                        271: (2, "ptr"), 281: (3, "int"), 441: (3, "ptr")},
             "aarch64": {22: (3, "int"), 72: (4, "ptr"), 73: (2, "ptr"), 441: (3, "ptr")}}


def no_deadline(arch: str, nr: int, args: list[str]) -> bool:
    """The poll has no timeout: a program waiting for a key waits for good (ssh, a
    REPL, a server's console), while a download or a sleep polls with a deadline.
    note: tells ssh from curl by the deadline alone; ssh with ServerAliveInterval
    polls with one and counts as running."""
    at, kind = DEADLINES.get(arch, {}).get(nr, (None, None))
    if at is None or at >= len(args):
        return False
    try:
        v = int(args[at], 16)
    except ValueError:
        return False
    return v == 0 if kind == "ptr" else v in (0xffffffff, 0xffffffffffffffff)


def args_of(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode(errors="replace")[:200] for a in raw.split(b"\0")[:-1][:10]]


def uptime() -> float:
    return float(Path("/proc/uptime").read_text().split()[0])


def shell_names(shell: dict | None) -> set[str]:
    """What counts as a shell in the foreground: /etc/shells, plus its login shell."""
    try:
        lines = Path("/etc/shells").read_text().split("\n")
    except OSError:
        lines = []
    found = {os.path.basename(ln.strip()) for ln in lines if ln.startswith("/")}
    return found | ({shell["name"]} if shell else set())


def held(paths: set[str]) -> set[str]:
    """Which of these files a process still has open."""
    found = set()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            fds = os.scandir(f"/proc/{entry.name}/fd")
            found |= {os.readlink(fd.path) for fd in fds} & paths
        except OSError:
            continue
    return found


# ---- raw logs --------------------------------------------------------------------------

def log_path(tid: str) -> Path:
    return LOGS / f"{tid}.raw"


def parts(tid: str) -> list[tuple[int, int, Path]]:
    """A raw log's rotated parts, (from, to, file), oldest first: <id>.<from>-<to>.raw
    while it is gzipped, then <id>.<from>-<to>.raw.gz."""
    found = {}
    for f in LOGS.glob(f"{tid}.*-*.raw*"):
        m = re.fullmatch(rf"{tid}\.(\d+)-(\d+)\.raw(\.gz)?", f.name)
        if m and (int(m[1]) not in found or not m[3]):
            found[int(m[1])] = (int(m[1]), int(m[2]), f)
    return [found[k] for k in sorted(found)]


def log_base(tid: str) -> int:
    """Where the live LOGS/<id>.raw starts in the whole log."""
    return max((to for _, to, _ in parts(tid)), default=0)


def log_size(tid: str) -> int:
    """The live file's size: what the marks and a run's offsets count in."""
    try:
        return log_path(tid).stat().st_size
    except OSError:
        return 0


def read_log(tid: str, since: int, until: int | None = None) -> bytes:
    try:
        with open(log_path(tid), "rb") as f:
            f.seek(since)
            return f.read() if until is None else f.read(max(until - since, 0))
    except OSError:
        return b""


def read_whole(tid: str, since: int, count: int) -> tuple[bytes, int] | None:
    """Up to `count` bytes from offset `since` of the whole log, rotated parts included,
    and the whole log's size; None when there is no log of that id."""
    with LOGS_LOCK:
        if not log_path(tid).exists() and not parts(tid):
            return None
        base = log_base(tid)
        size = base + log_size(tid)
        since = min(since, size)
        if since >= base:
            return read_log(tid, since - base, since - base + count), size
        for start, to, f in parts(tid):
            if start <= since < to:
                with (gzip.open(f, "rb") if f.suffix == ".gz" else open(f, "rb")) as src:
                    src.seek(since - start)
                    return src.read(min(count, to - since)), size
        return b"", size


def quiet_for(tid: str) -> int | None:
    """Seconds since the terminal last printed anything."""
    try:
        return max(int(time.time() - log_path(tid).stat().st_mtime), 0)
    except OSError:
        return None


def give(target: Path | int) -> None:
    """A file or folder the daemon made for it outside its home (or its open fd)
    becomes its own."""
    her = pwd.getpwnam(USER)
    (os.fchown if isinstance(target, int) else os.chown)(target, her.pw_uid, her.pw_gid)


# its side of write_hers: the folders, then the file from stdin (cat reads the pipe as it
# is; reopening /dev/stdin is refused it, root made the pipe), then its mode
WRITE_SH = 'mkdir -p -- "$(dirname -- "$1")" && (umask 077 && cat > "$1") && chmod "$2" -- "$1"'


def write_hers(path: Path, body, length: int, mode: int = 0o644) -> None:
    """`length` bytes read from `body` as its file at `path`, folders on the way too, all
    made by it (WRITE_SH): root never writes in its home, so nothing in it is root's (its
    browser shares it). Problem when the body ends early or the write fails."""
    proc = subprocess.Popen(["sh", "-c", WRITE_SH, "sh", str(path), f"{mode:o}"],
                            stdin=subprocess.PIPE, stderr=subprocess.PIPE, **AS_HER)
    left = length
    try:
        while left and (chunk := body.read(min(left, 1 << 20))):
            proc.stdin.write(chunk)
            left -= len(chunk)
    except BrokenPipeError:                         # install gave up: it says why
        pass
    finally:                                        # closes its stdin, waits for its end
        said = proc.communicate()[1].decode(errors="replace").strip()
    if proc.returncode:
        raise Problem(f"The file could not be written: {said}", 500)
    if left:
        raise Problem("The upload ended early.")


def make_dirs() -> None:
    """At start: the folders of the raw logs and of its runs."""
    LOGS.mkdir(parents=True, exist_ok=True)
    LOGS.chmod(0o711)                   # it may not list it, nor delete in it
    CMD.mkdir(parents=True, exist_ok=True)
    give(CMD)


def new_log(tid: str) -> None:
    """Root's file; its group may only write: its terminal's pipe appends to it as it."""
    log_path(tid).write_bytes(b"")
    os.chown(log_path(tid), 0, pwd.getpwnam(USER).pw_gid)
    log_path(tid).chmod(0o620)


def pipe(name: str, tid: str) -> None:
    """The pane's output into its raw log. A pipe already open is switched at once:
    nothing printed in between is lost."""
    tmux("pipe-pane", "-O", "-t", target(name), f"exec cat >> {log_path(tid)}")


def gzip_file(src: Path, hers: bool = False) -> None:
    """src into src.gz; src goes once the .gz is whole. `hers`: the .gz is its own."""
    tmp = src.with_name(src.name + ".gz.part")
    with open(src, "rb") as a, gzip.open(tmp, "wb") as b:
        shutil.copyfileobj(a, b)
    if hers:
        give(tmp)
    tmp.rename(src.with_name(src.name + ".gz"))
    src.unlink()


def rotate(t: Terminal, name: str) -> None:
    """A live raw log past LOG_ROTATE: moved to <id>.<from>-<to>.raw once the pane's
    old writer let go of it, the pipe switched to a fresh <id>.raw, then gzipped."""
    if log_size(t.id) < LOG_ROTATE:
        return
    with LOGS_LOCK, t.scan_lock:
        base, aside = log_base(t.id), LOGS / f"{t.id}.rotating"
        log_path(t.id).rename(aside)
        new_log(t.id)
        pipe(name, t.id)
        began = time.monotonic()                    # cat drains its pipe and ends
        while held({str(aside)}) and time.monotonic() - began < DRAIN_WAIT:
            time.sleep(TICK)
        part = LOGS / f"{t.id}.{base}-{base + aside.stat().st_size}.raw"
        aside.rename(part)
        t.pos, t.carry = 0, b""
    gzip_file(part)


# ---- its runs' files -------------------------------------------------------------------

def cmd_file(cid: str, kind: str) -> Path:
    return CMD / f"{cid}.{kind}"


def cmd_log(cid: str) -> Path:
    """A run's log: <id>.log, or <id>.log.gz once gzipped."""
    plain = cmd_file(cid, "log")
    gz = plain.with_name(plain.name + ".gz")
    return gz if gz.exists() and not plain.exists() else plain


def started_at(cid: str) -> float | None:
    try:
        return float(cmd_file(cid, "start").read_text().split("\n", 1)[0])
    except (OSError, ValueError):
        return None


def ended(cid: str) -> tuple[int, float] | None:
    """A run's exit status and its seconds, once it ended."""
    try:
        status, seconds = cmd_file(cid, "exit").read_text().split()
        return int(status), float(seconds)
    except (OSError, ValueError):
        return None


def echo(t: Terminal, tty: str, cid: str, shell_pid: int, sync: int) -> None:
    """A run's log into its terminal as it grows (the run writes the log only), until
    the run ended and all of it is there, the shell is gone, or its next run began (a run
    that ended with no .exit, state ended, has nothing more to copy); then a line into the
    fifo the run waits on before its prompt. `sync`: that fifo, open (read and write)
    until the run opened it too; the run opens it before its command, so by its .exit
    the line has a reader."""
    pos = 0
    try:
        with open(tty, "wb", buffering=0) as out:
            while True:
                over = ended(cid) is not None or not alive(shell_pid) \
                    or (t.cmd or {}).get("id") != cid
                try:
                    with open(cmd_file(cid, "log"), "rb") as f:
                        f.seek(pos)
                        data = f.read()
                except OSError:
                    data = b""
                if data:
                    out.write(data)
                    pos += len(data)
                elif over:
                    break
                time.sleep(ECHO_TICK)
    except OSError:
        pass
    finally:
        try:
            os.write(sync, b"\n")
        finally:
            os.close(sync)
            cmd_file(cid, "sync").unlink(missing_ok=True)


def start_echo(t: Terminal, name: str, cid: str, shell_pid: int) -> None:
    """The fifo of a run and the thread that copies its log into the terminal."""
    path = cmd_file(cid, "sync")
    os.mkfifo(path, 0o600)
    give(path)
    sync = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    threading.Thread(target=echo, args=(t, pane(name)["tty"], cid, shell_pid, sync),
                     daemon=True).start()


def at_prompt(tty: str) -> bool:
    """The shell reads its own command line: a line editor (bash's readline, zsh's zle)
    turns canonical mode and echo off and disables Ctrl-V (VLNEXT), which it handles
    itself. A command reading the terminal leaves VLNEXT alone: `cat`, `input()`, and
    bash's `read`, even `read -n1` and `read -s` that turn canonical mode or echo off.
    `read -e` reads with readline and looks like the prompt."""
    try:
        fd = os.open(tty, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            _, _, _, lflag, _, _, cc = termios.tcgetattr(fd)
            return not lflag & (termios.ICANON | termios.ECHO) \
                and cc[termios.VLNEXT] in (0, b"\0")
        finally:
            os.close(fd)
    except (OSError, termios.error):
        return False


def clean(raw: bytes) -> str:
    """A log's bytes as it reads them: no escape sequences, and of a line redrawn with
    returns (a progress bar) only its last state."""
    text = INVISIBLE.sub(b"", raw).decode(errors="replace")
    return "\n".join(ln.rstrip("\r").rsplit("\r", 1)[-1] for ln in text.split("\n"))


def newlines(f, until: int) -> int:
    """Line breaks in the first `until` bytes of an open file."""
    f.seek(0)
    count, left = 0, until
    while left > 0 and (chunk := f.read(min(left, 1 << 20))):
        count += chunk.count(b"\n")
        left -= len(chunk)
    return count


def excerpt(path: Path, chars: int) -> tuple[str, str | None, dict | None, int]:
    """The whole log, or when it is longer than `chars`: its start and its end, cut at
    line breaks, a HEAD to TAIL share each. Returns the head, the tail, the cut (the
    lines left out, numbered as sed numbers them, the total, and the byte offset they
    start at) and the log's size."""
    try:
        f = open(path, "rb")
    except OSError:
        return "", None, None, 0
    with f:
        size = os.fstat(f.fileno()).st_size
        if size <= chars:
            return clean(f.read()).rstrip("\n"), None, None, size
        head_n = chars * HEAD // (HEAD + TAIL)
        head = f.read(head_n)
        if b"\n" in head:
            head = head[:head.rindex(b"\n") + 1]
        f.seek(size - (chars - head_n))
        tail = f.read()
        if b"\n" in tail[:-1]:
            tail = tail[tail.index(b"\n") + 1:]
        hidden_to = size - len(tail)            # the hidden bytes: len(head) to here
        before_end = newlines(f, hidden_to)
        f.seek(hidden_to - 1)
        last = before_end + (f.read(1) != b"\n")
        total = newlines(f, size) + (tail[-1:] != b"\n")
    cut = {"first": head.count(b"\n") + 1, "last": last, "total": total, "from": len(head)}
    return clean(head).rstrip("\n"), clean(tail).rstrip("\n"), cut, size


def page(path: Path, since: int, chars: int) -> tuple[str, int | None, int]:
    """The log from byte `since` on, at most `chars` bytes ending at a line break when
    more follows; the offset the next page starts at (None at the end) and the size."""
    try:
        if path.suffix == ".gz":
            with open(path, "rb") as raw:   # gzip keeps the size in its last 4 bytes
                raw.seek(-4, 2)
                size = int.from_bytes(raw.read(4), "little")
            f = gzip.open(path, "rb")
        else:
            f = open(path, "rb")
            size = os.fstat(f.fileno()).st_size
    except OSError:
        return "", None, 0
    with f:
        since = min(since, size)
        f.seek(since)
        chunk = f.read(chars)
    more = since + len(chunk) < size
    if more and b"\n" in chunk:
        chunk = chunk[:chunk.rindex(b"\n") + 1]
    return clean(chunk).rstrip("\n"), since + len(chunk) if more else None, size


def housekeep() -> None:
    """At most every HOUSEKEEP_EVERY seconds: big raw logs rotated, the logs of runs that
    ended CMD_GZIP_AFTER seconds ago gzipped. Nothing is ever deleted."""
    global housekept
    if time.monotonic() - housekept < HOUSEKEEP_EVERY:
        return
    housekept = time.monotonic()
    for name, t in list(terms.items()):
        try:
            rotate(t, name)
        except (OSError, Problem):
            pass
    old = [f.with_suffix(".log") for f in CMD.glob("*.exit")
           if time.time() - f.stat().st_mtime > CMD_GZIP_AFTER]
    old = [f for f in old if f.exists()]
    if not old:
        return
    busy = held({str(f) for f in old})              # a background program still writes
    for f in old:
        if str(f) not in busy:
            try:
                gzip_file(f, hers=True)
            except OSError:
                pass


# ---- its terminals -------------------------------------------------------------------

def check_name(name) -> str:
    if not isinstance(name, str) or not NAME.match(name):
        raise Problem("A terminal name is 1 to 16 lowercase letters, digits, - or _, "
                      "starting with a letter or digit.")
    return name


def check_server(notes: list[str]) -> None:
    """Terminals this daemon opened, and no tmux server: something killed it (its own
    kill-server, out of memory). The box itself did not restart, or this daemon would
    know of no terminals. Said once per call until a call opens a new terminal."""
    global tmux_gone
    if terms and tmux_ok("list-sessions").returncode != 0:
        terms.clear()
        tmux_gone = True
    if tmux_gone:
        notes.append("tmux_gone")


def open_terminal(name: str, owner: str, notes: list[str]) -> Terminal:
    """The agent's login shell, as a login shell, in a new terminal. The shell learns the
    terminal's id, name and owner from its environment (the greeting, the prompt)."""
    shell = pwd.getpwnam(USER).pw_shell
    if not os.access(shell, os.X_OK):
        notes.append(f"login_shell_missing:{shell}")
        shell = "/bin/bash"
    t = Terminal(owner)
    new_log(t.id)
    tmux("new-session", "-d", "-s", name, "-x", str(COLS), "-y", str(ROWS), "-c", str(HOME),
         "-e", f"SHELLBOX_ID={t.id}", "-e", f"SHELLBOX_TERMINAL={name}",
         "-e", f"SHELLBOX_OWNER={owner}", "--", "/bin/bash", "-c", LAUNCH, shell)
    tmux("resize-window", "-t", target(name), "-x", str(COLS), "-y", str(ROWS))
    pipe(name, t.id)
    terms[name] = t
    return t


def doing(f: dict) -> str:
    """What a terminal does, from its facts, for a sentence."""
    if f["program"]:
        return "runs " + " ".join(f["front"][0]["args"] or [f["program"]])[:80]
    if f["busy"]:
        return "runs a command"
    return f"is at its prompt, quiet {(f['quiet_for'] or 0) // 60} min"


def make_room() -> str:
    """At MAX_TERMINALS, inside TERMS_LOCK: closes the one of its own terminals whose
    shell waits at its prompt and has printed nothing the longest, IDLE_CLOSE seconds at
    least. -> its name. Problem naming each open terminal and what it runs when none
    is."""
    rows = []
    for name in live():
        t = known(name)
        try:
            rows.append((name, t, facts(t, name, pane(name))))
        except Problem:
            continue                                    # gone in between
    idle = [(f["quiet_for"], name) for name, t, f in rows
            if t.owner == "celine" and not f["busy"] and not f["paused"]
            and (f["quiet_for"] or 0) >= IDLE_CLOSE]
    if not idle:
        said = "; ".join(f"{name} {doing(f)}" + ("" if t.owner == "celine" else
                                                f" ({t.owner}'s)") for name, t, f in rows)
        raise Problem(f"There are already {MAX_TERMINALS} terminals open and none of yours "
                      f"has waited at its prompt for {IDLE_CLOSE // 60} min: {said}.",
                      full=True, terminals=[name for name, _, _ in rows])
    _, name = max(idle)
    tmux("kill-session", "-t", target(name))
    terms.pop(name, None)
    return name


def ensure(name: str, owner: str, notes: list[str]) -> tuple[Terminal, bool]:
    """The terminal of this name, opened when there is none (its first prompt drawn)."""
    global tmux_gone
    with TERMS_LOCK:
        check_server(notes)
        opened = not exists(name)
        if opened:
            if len(live()) >= MAX_TERMINALS:
                notes.append(f"made_room:{make_room()}")
            open_terminal(name, owner, notes)
            tmux_gone = False
        t = known(name)
    if opened:
        # the greeting and the first prompt: the shell alone, reading the terminal
        pid, began = pane(name)["pid"], time.monotonic()
        while time.monotonic() - began < PROMPT_WAIT:
            time.sleep(TICK)
            _, job, front = in_front(t, {"pid": pid})
            if not front and reads_terminal(job):
                break
    return t, opened


def existing(name, notes: list[str]) -> Terminal:
    check_name(name)
    with TERMS_LOCK:
        check_server(notes)
    if not exists(name):
        terms.pop(name, None)
        raise Problem(f"No terminal named {name}.", 404, terminals=names(), notes=notes)
    return known(name)


def remove_terminal(name: str) -> None:
    with TERMS_LOCK:
        if exists(name):
            tmux("kill-session", "-t", target(name))
        terms.pop(name, None)


def refresh(t: Terminal, name: str, p: dict, notes: list[str]) -> None:
    """At the start of every call: its size again, and the raw log still recording."""
    if p["dead"]:
        return
    tmux("resize-window", "-t", target(name), "-x", str(COLS), "-y", str(ROWS))
    if not p["pipe"] or not log_path(t.id).exists():
        if not log_path(t.id).exists():
            new_log(t.id)
        pipe(name, t.id)
        notes.append("pipe_rearmed")


def deliver(name: str, keys: list[str], text: str, enter: bool, fullscreen: bool) -> None:
    """Keys first, then the text as one paste, then Enter, only after text."""
    for key in keys:
        if len(key) == 1:
            # as bytes: tmux would read a lone ";" as the end of its command
            tmux("send-keys", "-t", target(name), "-H", *(f"{b:02x}" for b in key.encode()))
        else:
            tmux("send-keys", "-t", target(name), key[:-1] + "\\;" if key.endswith(";") else key)
    if text:
        # through a tmux buffer: any length, byte for byte. At a prompt, -p pastes as a
        # paste when the program asked for that, so a TAB in a heredoc stays a TAB. A
        # full-screen program gets it as typed: vim would insert a paste even after Escape.
        tmux("load-buffer", "-b", f"typing-{name}", "-", stdin=text.encode())
        tmux("paste-buffer", "-d", *([] if fullscreen else ["-p"]), "-b", f"typing-{name}",
             "-t", target(name))
        if enter:
            tmux("send-keys", "-t", target(name), "Enter")


def cursor_line(name: str, p: dict) -> str:
    lines = tmux("capture-pane", "-p", "-t", target(name)).split("\n")
    return lines[p["row"]].rstrip() if p["row"] < len(lines) else ""


def hung_up(conn) -> bool:
    if conn is None:
        return False
    try:
        conn.setblocking(False)
        try:
            return conn.recv(1, socket.MSG_PEEK) == b""
        finally:
            conn.setblocking(True)
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True


def alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def settle(tid: str, since: int) -> None:
    """Until the terminal printed nothing for SETTLE seconds, at most SETTLE_MAX."""
    began = quiet = time.monotonic()
    size = since
    while time.monotonic() - began < SETTLE_MAX and time.monotonic() - quiet < SETTLE:
        time.sleep(TICK)
        if (now := log_size(tid)) != size:
            size, quiet = now, time.monotonic()


def trim(lines: list[str]) -> list[str]:
    lines = [ln.rstrip() for ln in lines]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def screen(name: str) -> tuple[str, int | None]:
    """The whole screen, and the one row shown in reverse video if there is exactly one
    (the highlighted entry of a menu)."""
    plain = tmux("capture-pane", "-p", "-t", target(name)).split("\n")
    styled = tmux("capture-pane", "-p", "-e", "-t", target(name)).split("\n")
    rows = [i for i, ln in enumerate(styled[:ROWS]) if REVERSE.search(ln)]
    return "\n".join(trim(plain)), (rows[0] + 1 if len(rows) == 1 else None)


# ---- what it gets to read -----------------------------------------------------------

def facts(t: Terminal, name: str, p: dict) -> dict:
    """Where the terminal is: its shell, directory, and what runs in front."""
    if p["dead"]:
        return {"shell": t.shell or None, "paused": False,
                "front": [], "program": None, "pid": None, "busy": False,
                "waiting": False, "running_for": None, "quiet_for": quiet_for(t.id)}
    t.scan()
    shell, job, front = in_front(t, p)
    if shell and shell["name"] != t.shell:
        if t.shell:                # `exec zsh`: bash's open mark will never close
            t.open = False
        t.shell = shell["name"]
    started = started_at(t.cmd["id"]) if t.cmd and not ended(t.cmd["id"]) else None
    if front:
        leader = next((g for g in front if shell and g["pid"] == shell["tpgid"]),
                      min(front, key=lambda g: g["start"]))
        running_for = uptime() - leader["start"]
    else:
        running_for = time.time() - started if t.open and started else None
    # the shell alone at its own prompt: nothing runs, whatever mark is open
    alone = not front and bool(job) and reads_terminal(job) and at_prompt(p["tty"])
    return {"shell": shell["name"] if shell else t.shell or None, "cwd": p["cwd"],
            "front": [{"pid": g["pid"], "name": g["name"], "args": args_of(g["pid"])}
                      for g in front],
            "program": front[0]["name"] if front else None,
            "pid": front[0]["pid"] if front else None,
            "busy": bool(front) or (t.open and not alone), "paused": t.paused is not None,
            "waiting": bool(job) and (bool(front) or t.open) and reads_terminal(job)
            and not alone,
            "running_for": None if running_for is None else max(int(running_for), 0),
            "quiet_for": quiet_for(t.id)}


def base(t: Terminal, name: str, notes: list[str], opened: bool = False) -> dict:
    """The fields every answer about one terminal carries."""
    return {"terminal": name, "id": t.id, "owner": t.owner, "opened": opened,
            "notes": notes, "boot_id": STATUS.get("boot_id"), "fresh": STATUS.get("fresh"),
            "started": STATUS.get("started"), "boot": STATUS.get("boot")}


def her_notes(name: str, notes: list[str]) -> None:
    notes.extend(pending_notes.pop(name, []))       # what the user did meanwhile
    notes.extend(home_note())


def where(t: Terminal, name: str) -> dict:
    """The facts, or the closed ones when the terminal is gone."""
    if not exists(name):
        return {"shell": t.shell or None}
    p = pane(name)
    return {**facts(t, name, p), "fullscreen": p["fullscreen"], "cursor": [p["row"] + 1, p["col"] + 1],
            "dead": p["dead"], "exit": p["exit"] if p["dead"] else None}


def words(command: str) -> list[str]:
    """The command line's words, its operators (; & | ( ) < >) as words of their own."""
    lex = shlex.shlex(command.replace("\n", ";").replace("`", ";"), posix=True,
                      punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:                  # an open quote
        return command.split()


def reaches_net(command: str, depth: int = 0) -> str | None:
    """The URL or the program by which this command line reaches the net."""
    if url := URL_IN.search(command):
        return url.group()
    seg: list[str] = []
    for w in [*words(command), ";"]:
        if w and set(w) <= OPS:
            i = 0
            while i < len(seg) and (seg[i] in LEAD or LEADING.match(seg[i])):
                i += 1
            prog, rest = os.path.basename(seg[i]) if i < len(seg) else "", seg[i + 1:]
            if prog.startswith("python") and rest[:1] == ["-m"]:
                prog, rest = rest[1] if len(rest) > 1 else "", rest[2:]
            if prog in NET or set(rest) & NET_SUB.get(prog, set()):
                return prog
            if prog in SHELLS and "-c" in rest[:-1] and depth < 3:
                if hit := reaches_net(rest[rest.index("-c") + 1], depth + 1):
                    return hit
            seg = []
        else:
            seg.append(w)
    return None


def tagged(command: str, cwd: str | None) -> str | None:
    """Where the first path this command line names that carries the tag came from."""
    for w in words(command)[:200]:
        for v in {w, w.split("=", 1)[-1]}:
            if not v or set(v) <= OPS:
                continue
            v = re.sub(r"^~(?=/|$)", str(HOME), v).replace("${HOME}", str(HOME))
            p = os.path.join(cwd or HOME, v.replace("$HOME", str(HOME)))
            for q in itertools.islice(glob.iglob(p), 100) if set(v) & set("*?[") else [p]:
                try:
                    return os.getxattr(q, ORIGIN).decode(errors="replace")
                except OSError:
                    pass
    return None


def outside(command: str | None, cwd: str | None) -> tuple[bool, str | None]:
    """Whether a run's output is text from the net, and where it came from."""
    source = (reaches_net(command) or tagged(command, cwd)) if command else None
    return source is not None, source


def run_answer(t: Terminal, name: str, cid: str, state: str, notes: list[str],
               opened: bool = False, chars: int | None = None) -> dict:
    """A run's answer: how it went, its log, and with `chars` its output (head and tail
    when long)."""
    res = {**base(t, name, notes, opened), **where(t, name), "state": state,
           "cmd": cid, "command": t.cmd["command"] if t.cmd else None,
           "log": str(cmd_log(cid)), "exit": None, "seconds": None}
    done = ended(cid)
    if done:
        res.update(state="done", exit=done[0], seconds=done[1])
    elif began := started_at(cid):
        res["seconds"] = round(time.time() - began, 1)
    if chars:
        head, tail, cut, size = excerpt(cmd_log(cid), chars)
        res.update(output=head, tail=tail, cut=cut, size=size)
    res["outside"], res["outside_from"] = outside(res["command"], res.get("cwd"))
    # the guard's egress fact: the URL or program by which it reached the net
    res["net"] = reaches_net(res["command"]) if res["command"] else None
    her_notes(name, notes)
    return res


def screen_answer(t: Terminal, name: str, notes: list[str], opened: bool = False) -> dict:
    """The screen, what runs in front and its latest run."""
    res = {**base(t, name, notes, opened), **where(t, name), "screen": "",
           "highlighted": None}
    if not exists(name):
        res["state"] = "closed"
    else:                              # a closed terminal still shows its last screen
        res["screen"], res["highlighted"] = screen(name)
        res["state"] = ("closed" if res["dead"] else "fullscreen" if res["fullscreen"]
                        else "waiting" if res["waiting"] else "running" if res["busy"]
                        else "prompt")
    res["cmd"] = latest(t)
    her_notes(name, notes)
    return res


def latest(t: Terminal) -> dict | None:
    """The agent's latest run in this terminal, in short."""
    if not t.cmd:
        return None
    done = ended(t.cmd["id"])
    return {"id": t.cmd["id"], "command": t.cmd["command"], "log": str(cmd_log(t.cmd["id"])),
            "exit": done[0] if done else None, "seconds": done[1] if done else None}


def check_chars(value) -> int:
    if value is None:
        return HEAD + TAIL
    if isinstance(value, bool) or not isinstance(value, int) or value < 200:
        raise Problem("chars is a whole number, at least 200.")
    return min(value, PAGE_MAX)


def busy(t: Terminal, name: str, p: dict) -> dict | None:
    """Why a new run cannot go into this terminal now: it would go to what runs there."""
    if t.paused:
        _, _, front = in_front(t, p)
        return {"reason": "paused", "program": front[0]["name"] if front else None}
    f = facts(t, name, p)
    if f["busy"]:
        pending = t.cmd and not ended(t.cmd["id"])
        return {"reason": "running", "cmd": t.cmd["id"] if pending else None,
                "command": t.cmd["command"] if pending else None,
                "program": f["program"], "pid": f["pid"], "running_for": f["running_for"],
                "waiting": f["waiting"]}
    if CONTINUATION.match(cursor_line(name, p)):
        return {"reason": "more_input"}
    return None


def run_call(body: dict, progress, conn) -> dict:
    name = check_name(body.get("terminal") or "main")
    command = body.get("command")
    if not isinstance(command, str) or not command.strip():
        raise Problem("command is the command line to run.")
    timeout = body.get("timeout", RUN_MAX)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise Problem("timeout is a number of seconds.")
    timeout, chars = min(float(timeout), RUN_MAX), check_chars(body.get("chars"))
    owner = check_owner(body.get("owner"))
    notes: list[str] = []
    t, opened = ensure(name, owner, notes)
    with t.lock:
        p = pane(name)
        if p["dead"]:
            res = screen_answer(t, name, notes)
            remove_terminal(name)
            return res
        refresh(t, name, p, notes)
        refused = busy(t, name, p)
        if refused:
            res = {**base(t, name, notes, opened), **where(t, name), **refused,
                   "state": "busy", "running": latest(t)}
            her_notes(name, notes)
            return res
        cid = new_id()
        t.cmd = {"id": cid, "command": command}
        start_echo(t, name, cid, p["pid"])
        deliver(name, [], f" __shellbox_run {cid} {shlex.quote(command)}", True, False)
    progress({"started": cid})
    state = watch_run(t, name, p["pid"], cid, timeout, progress, conn)
    res = run_answer(t, name, cid, state, notes, opened, chars)
    if state == "closed":
        remove_terminal(name)
    return res


def wait_call(body: dict, progress, conn) -> dict:
    """The latest run in a terminal, watched as /run watches its own."""
    name = check_name(body.get("terminal"))
    timeout = body.get("timeout", RUN_MAX)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise Problem("timeout is a number of seconds.")
    timeout, chars = min(float(timeout), RUN_MAX), check_chars(body.get("chars"))
    notes: list[str] = []
    t = existing(name, notes)
    p = pane(name)
    refresh(t, name, p, notes)
    if not t.cmd:
        return {**screen_answer(t, name, notes), "no_run": True}
    cid = t.cmd["id"]
    state = "done" if ended(cid) else "closed" if p["dead"] else \
        watch_run(t, name, p["pid"], cid, timeout, progress, conn)
    res = run_answer(t, name, cid, state, notes, chars=chars)
    if state == "closed":
        remove_terminal(name)
    return res


def watch_run(t: Terminal, name: str, shell_pid: int, cid: str, timeout: float,
              progress=None, conn=None) -> str:
    """Watches a run until one of the rules at the top of this file says what it is."""
    began = time.monotonic()
    waiting_since, alone_since, last_line, progress_at = None, None, "", began
    tty = pane(name)["tty"]
    while True:
        time.sleep(TICK)
        now = time.monotonic()
        if hung_up(conn):
            raise HungUp()
        if ended(cid):
            back(t, tty, shell_pid)
            return "done"
        if not alive(shell_pid):
            return "closed"
        if started_at(cid) is None:
            if now - began >= START_WAIT:
                return "not_started"
            continue
        _, job, front = in_front(t, {"pid": shell_pid})
        reading = reads_terminal(job)
        alone_since = (alone_since or now) if not front and reading and at_prompt(tty) \
            else None
        if alone_since and now - alone_since >= SETTLE:
            return "ended"
        waiting_since = (waiting_since or now) if reading and not alone_since else None
        if waiting_since and now - waiting_since >= WAITING_FOR:
            return "waiting"
        if now - began >= timeout:
            return "running"
        if progress and now - progress_at >= PROGRESS_EVERY:
            progress_at = now
            line = last_output_line(cid)
            if line and line != last_line:
                last_line = line
                progress({"progress": line})


def back(t: Terminal, tty: str, shell_pid: int) -> None:
    """After a run's .exit: until the shell drew its prompt (the D mark, or alone at its
    prompt), at most SYNC_WAIT seconds plus a TICK; the echo of its output comes first."""
    began = time.monotonic()
    while time.monotonic() - began < SYNC_WAIT + TICK and alive(shell_pid):
        t.scan()
        if not t.open:
            return
        _, job, front = in_front(t, {"pid": shell_pid})
        if not front and reads_terminal(job) and at_prompt(tty):
            return
        time.sleep(ECHO_TICK)


def last_output_line(cid: str) -> str:
    try:
        with open(cmd_file(cid, "log"), "rb") as f:
            f.seek(max(f.seek(0, 2) - 4096, 0))
            tail = f.read()
    except OSError:
        return ""
    lines = [ln.strip() for ln in clean(tail).split("\n")]
    return next((ln for ln in reversed(lines) if ln), "")[:COLS]


def check_owner(owner) -> str:
    owner = owner or "celine"
    if not isinstance(owner, str) or not NAME.match(owner):
        raise Problem("owner is a short name, like a terminal's.")
    return owner


def watch_session(session: str) -> dict:
    """A coding session shown live: its terminal code-<end of its id> runs the agent's
    attach command; one that is open is reused. -> {"terminal", "terminal_id"}, or
    {"terminal_error"}: the session itself is running either way."""
    name = "code-" + re.sub(r"[^a-z0-9]", "", session.lower())[-8:]
    try:
        if not exists(name):
            type_call({"terminal": name, "text": CODE.attach(session), "owner": "celine"})
        return {"terminal": name, "terminal_id": known(name).id}
    except Problem as e:
        return {"terminal_error": str(e)}


def type_call(body: dict) -> dict:
    name = check_name(body.get("terminal") or "main")
    text, keys = body.get("text") or "", body.get("keys") or []
    if not isinstance(text, str):
        raise Problem("text is a string.")
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise Problem("keys is a list of key names.")
    bad = [k for k in keys
           if not (k in NAMED_KEYS or CTRL_ALT.match(k) or len(k) == 1)]
    if bad:
        raise Problem(f"Unknown key {bad[0]}. Keys are {', '.join(sorted(NAMED_KEYS))}, "
                      "C- or M- with one character, or one single character.")
    enter = body.get("enter", True)
    if not isinstance(enter, bool):
        raise Problem("enter is true or false.")
    notes: list[str] = []
    t, opened = ensure(name, check_owner(body.get("owner")), notes)
    with t.lock:
        p = pane(name)
        if p["dead"]:
            res = screen_answer(t, name, notes)
            remove_terminal(name)
            return res
        refresh(t, name, p, notes)
        start = log_size(t.id)
        if text or keys:
            deliver(name, keys, text, enter and bool(text), p["fullscreen"])
    if text or keys:
        settle(t.id, start)
    res = screen_answer(t, name, notes, opened)
    if res["state"] == "closed":
        remove_terminal(name)
    return res


def stop_call(body: dict) -> dict:
    """Ctrl-C to what runs in front; what outlived the last one gets SIGTERM, then
    SIGKILL. The run it ends is answered like a run; else the screen."""
    notes: list[str] = []
    name = body.get("terminal") or "main"
    t = existing(name, notes)
    chars = check_chars(body.get("chars"))
    killed = None
    with t.lock:
        p = pane(name)
        start = log_size(t.id)
        stops_run = t.cmd and not ended(t.cmd["id"])
        _, _, front = in_front(t, p)
        paused = t.paused is not None   # a stopped program cannot react to Ctrl-C
        again = [] if paused else [g for g in front if g["pid"] in t.survived]
        if again:
            killed = {"signal": kill_job(again), "names": sorted({g["name"] for g in again})}
        else:
            deliver(name, ["C-c"], "", False, p["fullscreen"])
    if stops_run:
        state = watch_run(t, name, p["pid"], t.cmd["id"], STOP_WAIT)
        res = run_answer(t, name, t.cmd["id"], state, notes, chars=chars)
    else:
        settle(t.id, start)
        res = screen_answer(t, name, notes)
    still = {g["pid"]: g["name"] for g in res.get("front") or []}
    with t.lock:
        t.survived = set() if killed or paused or t.paused else \
            {g["pid"] for g in front if g["pid"] in still}
        res["survived"] = sorted({still[pid] for pid in t.survived})
    res["killed"] = killed
    return res


def kill_job(job: list[dict]) -> str:
    """SIGTERM to each program, SIGKILL to those still there after KILL_WAIT seconds;
    the last signal sent."""
    def there(pid: int) -> bool:
        q = proc(pid)
        return q is not None and q["state"] != "Z"

    def send(sig: int, pids: list[int]) -> None:
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
    pids = [g["pid"] for g in job]
    send(signal.SIGTERM, pids)
    began = time.monotonic()
    while time.monotonic() - began < KILL_WAIT:
        if not any(there(pid) for pid in pids):
            return "SIGTERM"
        time.sleep(TICK)
    send(signal.SIGKILL, [pid for pid in pids if there(pid)])
    return "SIGKILL"


def read_call(body: dict) -> dict:
    notes: list[str] = []
    name = body.get("terminal")
    if not name:
        with TERMS_LOCK:
            check_server(notes)
        return listing(notes)
    t = existing(name, notes)
    since = body.get("from")
    if since is not None and (isinstance(since, bool) or not isinstance(since, int)
                              or since < 0):
        raise Problem("from is a byte offset, a whole number.")
    chars = check_chars(body.get("chars"))
    p = pane(name)
    refresh(t, name, p, notes)
    if since is None or p["fullscreen"] or not t.cmd:
        return screen_answer(t, name, notes)
    res = run_answer(t, name, t.cmd["id"], "closed" if p["dead"] else "running", notes)
    if res["state"] == "running" and res.get("waiting"):
        res["state"] = "waiting"
    text, nxt, size = page(cmd_log(t.cmd["id"]), since, chars)
    res.update({"output": text, "from": since, "next": nxt, "size": size})
    return res


def note(t: Terminal, name: str, code: str) -> None:
    """What the user did to one of its terminals, for its next call on it."""
    if t.owner == "celine":
        pending_notes.setdefault(name, []).append(code)


def send_bytes(name: str, raw: bytes) -> None:
    for i in range(0, len(raw), KEYS_CHUNK):
        tmux("send-keys", "-t", target(name), "-H",
             *(f"{b:02x}" for b in raw[i:i + KEYS_CHUNK]))


def keys_call(body: dict) -> dict:
    """Raw bytes into the user's own terminal, as typed at a keyboard. When Enter goes to
    the shell itself, the line it runs is read first, for the app's commands log."""
    name = check_name(body.get("terminal"))
    try:
        raw = base64.b64decode(body.get("data") or "", validate=True)
    except (binascii.Error, TypeError, ValueError):
        raise Problem("data is the base64 of the bytes to type.")
    if len(raw) > KEYS_MAX:
        raise Problem(f"At most {KEYS_MAX} bytes at once.")
    t = existing(name, [])
    if t.owner == "celine":
        raise Problem(f"{name} is Celine's terminal.")
    if t.paused:
        return {"refused": f"{name} is paused."}
    line = None
    with t.lock:
        p = pane(name)
        head, enter, rest = raw.partition(b"\r")
        if enter and not p["dead"] and not in_front(t, p)[2]:
            if head:
                start = log_size(t.id)
                send_bytes(name, head)
                began = time.monotonic()               # its echo
                while time.monotonic() - began < ECHO_WAIT and log_size(t.id) == start:
                    time.sleep(ECHO_TICK)
            text = cursor_line(name, pane(name))
            if text.startswith(f"{t.owner}@shellbox:") and "$" in text:
                text = text.split("$", 1)[1].strip()    # after the prompt's "$ "
            line = text or None
            send_bytes(name, enter + rest)
        else:
            send_bytes(name, raw)
    return {"ok": True, "line": line}


def control_call(body: dict) -> dict:
    """stop, pause, resume or close, from the user's page. A pause is what Ctrl-Z does,
    only uncatchable: the program's group gets SIGSTOP, the shell says "Stopped" and
    shows its prompt (tmux continues its pane's process whenever that one stops, so the
    shell cannot be frozen with it); resume types `fg`."""
    name = check_name(body.get("terminal"))
    action = body.get("action")
    if action not in ("stop", "pause", "resume", "close"):
        raise Problem("action is stop, pause, resume or close.")
    t = existing(name, [])
    p = pane(name)
    shell, _, front = in_front(t, p)
    program = front[0]["name"] if front else ""
    now = int(time.time())
    res = {"ok": True, "action": action, "terminal": name, "id": t.id,
           "program": program or None, "pid": front[0]["pid"] if front else None}
    if action in ("stop", "pause") and t.paused:
        raise Problem(f"{name} is paused.")
    if action == "stop":
        with t.lock:
            deliver(name, ["C-c"], "", False, p["fullscreen"])
        note(t, name, f"stopped:{program}:{now}")
    elif action == "pause":
        if not front or shell is None or shell["tpgid"] == shell["pgrp"]:
            raise Problem(f"No program runs in front of {name}.")
        os.killpg(shell["tpgid"], signal.SIGSTOP)
        t.paused = {"pgid": shell["tpgid"]}
        note(t, name, f"paused:{program}:{now}")
    elif action == "resume":
        if not t.paused:
            raise Problem(f"{name} is not paused.")
        with t.lock:
            deliver(name, [], "fg", True, False)
        t.paused = None
        note(t, name, f"resumed:{program}:{now}")
    else:
        if t.paused:        # nothing stays stopped behind a closed terminal
            os.killpg(t.paused["pgid"], signal.SIGCONT)
        lines = tmux("capture-pane", "-p", "-t", target(name)).split("\n")
        res["screen"] = "\n".join(trim(lines))
        # the note first: the next terminal of this name carries it
        note(t, name, f"closed:{now}")
        remove_terminal(name)
    return res


def listing(notes: list[str]) -> dict:
    """Every terminal, the ones whose shell exited too; those are reaped right after."""
    rows, dead = [], []
    for name in names():
        t = known(name)
        try:
            p = pane(name)
        except Problem:
            continue                                    # gone in between
        f = facts(t, name, p)
        rows.append({"name": name, "id": t.id, "owner": t.owner, "opened_at": t.opened_at,
                     "shell": f["shell"], "cwd": f.get("cwd"), "program": f["program"], "pid": f["pid"],
                     "front": f["front"], "busy": f["busy"],
                     "paused": f["paused"], "fullscreen": p["fullscreen"],
                     "waiting": f["waiting"],
                     "last_exit": t.last_d, "running_for": f["running_for"],
                     "quiet_for": f["quiet_for"], "cmd": latest(t),
                     "dead": p["dead"], "exit": p["exit"] if p["dead"] else None})
        if p["dead"]:
            dead.append(name)
    for name in dead:
        remove_terminal(name)
    housekeep()
    return {"terminals": rows, "boot_id": STATUS.get("boot_id"),
            "started": STATUS.get("started"), "fresh": STATUS.get("fresh"),
            "boot": STATUS.get("boot"), "notes": notes}


# ---- the user's live view ------------------------------------------------------------

# what tmux asks its client when it attaches (DA, DA2, XTVERSION, the colours). No answer
# can reach it here, and the page's xterm would answer into the user's shell as typed text.
QUERIES = (b"\x1b[c", b"\x1b[>c", b"\x1b[>q", b"\x1b]10;?\x1b\\", b"\x1b]11;?\x1b\\")


def unasked(chunk: bytes, carry: bytes = b"") -> tuple[bytes, bytes]:
    """The view's bytes without tmux's questions: what to send, and the tail kept back
    because a question may go on in the next chunk."""
    buf = carry + chunk
    for q in QUERIES:
        buf = buf.replace(q, b"")
    cut = buf.find(b"\x1b", max(0, len(buf) - 7))
    while cut >= 0:
        if any(q.startswith(buf[cut:]) for q in QUERIES):
            return buf[:cut], buf[cut:]
        cut = buf.find(b"\x1b", cut + 1)
    return buf, b""


def view(name: str, conn: socket.socket, write) -> None:
    """A read-only tmux client in a terminal of its size, its bytes sent as they come.
    tmux first draws the whole screen, so a viewer joining late sees it right away. A
    viewer that stops reading for VIEW_TIMEOUT seconds is dropped; the terminal never
    waits for it."""
    her = pwd.getpwnam(USER)
    pid, fd = pty.fork()
    if pid == 0:
        # the child: become it, then tmux. Whatever fails, it must never come back here,
        # or two processes would serve one connection.
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
            os.setgid(her.pw_gid)
            os.initgroups(USER, her.pw_gid)
            os.setuid(her.pw_uid)
            os.execvpe("tmux", [*TMUX, "attach-session", "-t", target(name), "-r",
                                "-f", "ignore-size"], {**ENV, "TERM": "xterm-256color"})
        finally:
            os._exit(127)
    conn.settimeout(VIEW_TIMEOUT)
    carry = b""
    try:
        while True:
            ready, _, _ = select.select([fd, conn], [], [], 30)
            if conn in ready and not conn.recv(1, socket.MSG_PEEK):
                return                                  # the viewer went away
            if fd in ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return
                out, carry = unasked(chunk, carry)
                if out:
                    write(out)
    except OSError:                                     # the terminal ended, or a stall
        return
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
        os.close(fd)


# ---- start, ~/boot.sh ------------------------------------------------------------------

def install_rc() -> None:
    """The marks and the run function, for every shell that sources /etc/bash.bashrc."""
    RC.write_text(RUN_SH.format(cmd=CMD, sync=SYNC_WAIT))
    RC.chmod(0o644)


def start() -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", KEY_HASH):
        sys.exit(f"shellboxd: {KEY_HASH_FILE} is not the key's sha256 (64 lowercase hex)")
    her = pwd.getpwnam(USER)
    # a missing home, or one that is not its own (a new volume, another uid), is given to
    # it: the folder only, what is in it keeps its owners
    # A home that cannot be written is refused, never worked around: home-disk.sh makes
    # the host folder under an unmounted home disk immutable, so a disk that failed to
    # mount stops its box here instead of letting it write onto the host's disk.
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        st = HOME.stat()
        if (st.st_uid, st.st_gid) != (her.pw_uid, her.pw_gid):
            os.chown(HOME, her.pw_uid, her.pw_gid)
            print(f"shellboxd: {HOME} was {st.st_uid}:{st.st_gid}'s, now {USER}'s "
                  f"({her.pw_uid}:{her.pw_gid})", flush=True)
        check = HOME / ".shellboxd-write-check"
        check.touch()
        check.unlink()
    except OSError as e:
        sys.exit(f"shellboxd: {HOME} cannot be written ({e.strerror}); "
                 "is the home disk mounted? Not starting.")
    STATUS["started"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    STATUS["boot_id"] = f"{STATUS['started']}+{secrets.token_hex(2)}"
    # the marker lives in the container, not in its home: a restart keeps it, a reset not
    STATUS["fresh"] = not MARKER.exists()
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(STATUS["started"] + "\n")
    if STATUS["fresh"]:
        RESET.write_text(STATUS["started"] + "\n")
    STATUS["reset"] = RESET.read_text().strip() if RESET.is_file() else ""
    # a completely empty home gets the usual dotfiles, once; never again after that, so a
    # .bashrc it deleted stays deleted. Copied by it, so they are its own. cp -r, not -a:
    # -a would put skel's mode on its home.
    if not any(HOME.iterdir()):
        subprocess.run(["cp", "-r", "/etc/skel/.", str(HOME)], check=True, **AS_HER)
    make_dirs()
    install_rc()
    start_agent()
    STATUS["boot"] = {"state": "running", "exit": None, "seconds": None, "error": None}
    threading.Thread(target=boot, daemon=True).start()
    threading.Thread(target=measure_home, daemon=True).start()


def load_agent(name: str):
    """The coding agent backend named `name`: shellbox/agents/<name>.py as a module."""
    path = AGENTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"shellbox_agent_{name}", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def start_agent() -> None:
    """The coding agent's server, as it; a backend that fails to load is in STATUS and
    /code/* answers with it, the terminals are not touched."""
    global CODE
    try:
        CODE = load_agent(CODING_AGENT)
        CODE.boot(HOME, ENV, AS_HER, LLM_URL, LLM_KEY)
        STATUS["code"] = {"agent": CODING_AGENT, "error": None}
    except Exception as e:                      # noqa: BLE001
        traceback.print_exc()
        CODE = None
        STATUS["code"] = {"agent": CODING_AGENT, "error": f"{type(e).__name__}: {e}"}


def boot() -> None:
    """The package lists, then ~/boot.sh, in the background, so its terminals never wait
    for them, and its boot.sh installs from fresh lists (the order cloud-init keeps).
    Whatever goes wrong here ends up in STATUS as a failure, never as "running" forever."""
    update_packages()
    try:
        run_boot_script()
    except Exception as e:
        STATUS["boot"].update(state="failed", error=f"{type(e).__name__}: {e}")


def update_packages() -> None:
    """apt-get update at every start: the image ships no package lists, the reset drops
    its own, and installs need them. A slow or broken mirror only costs APT_LIMIT seconds;
    it can run it again itself."""
    began = time.monotonic()
    try:
        rc = subprocess.call(["timeout", "-k", "10", str(APT_LIMIT), "apt-get", "update",
                              "-qq"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except OSError as e:
        rc = f"{type(e).__name__}: {e}"
    STATUS["apt"] = {"exit": rc, "seconds": round(time.monotonic() - began)}


def caps() -> tuple[int, int]:
    """Its home's (soft, hard) caps in bytes."""
    hard = int(float(HOME_GB) * 1024 ** 3)
    soft = int(float(HOME_SOFT_GB) * 1024 ** 3) if HOME_SOFT_GB else hard * 9 // 10
    return min(soft, hard), hard


def measure_home() -> None:
    """The agent's home's size on disk every HOME_EVERY seconds (SHELLBOX_HOME_GB): for
    the greeting (USAGE), home_note() and put_file()."""
    while True:
        try:
            out = subprocess.run(["ionice", "-c3", "nice", "-n19", "du", "-sx",
                                  "--block-size=1", str(HOME)], capture_output=True,
                                 text=True, timeout=HOME_EVERY).stdout
            measured(int(out.split()[0]), int(time.time()))
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as e:
            STATUS["home"] = {"error": f"{type(e).__name__}: {e}"}
        time.sleep(HOME_EVERY)


def measured(used: int, at: int) -> None:
    """One measure of its home: `used` bytes at `at` (epoch seconds)."""
    soft, hard = caps()
    STATUS["home"] = {"used": used, "soft": soft, "limit": hard, "at": at}
    USAGE.write_text(f"{used} {hard}\n")


home_noted = None     # the measure the last home note came from


def home_note() -> list[str]:
    """home_over:<used>:<cap> on its next result after each measure that finds its home
    past a cap (the hard one once it is past that too): once per measure, not on every
    result."""
    global home_noted
    h = STATUS.get("home") or {}
    if not h.get("limit") or h.get("used", 0) <= h["soft"] or home_noted == h["at"]:
        return []
    home_noted = h["at"]
    cap = h["limit"] if h["used"] > h["limit"] else h["soft"]
    return [f"home_over:{h['used']}:{cap}"]


def home_room(length: int) -> None:
    """Refuses `length` more bytes past its home's hard cap, by the last measure and what
    landed since (Problem 413)."""
    h = STATUS.get("home") or {}
    if not h.get("limit") or h["used"] + length <= h["limit"]:
        return
    raise Problem(f"The home holds {h['used'] / 1024 ** 3:.1f} GB of its "
                  f"{h['limit'] / 1024 ** 3:g} GB and the file is "
                  f"{max(length // 1048576, 1)} MB: delete something first.", 413)


def run_boot_script() -> None:
    script = HOME / "boot.sh"
    if not script.is_file():
        STATUS["boot"].update(state="none")
        return
    log = HOME / "boot.log"
    began = time.monotonic()
    head = f"~/boot.sh started {STATUS['started']}\n".encode()
    write_hers(log, io.BytesIO(head), len(head))    # its own; the rest is appended to it
    with open(log, "a") as f:
        rc = subprocess.call(["timeout", "-k", "10", str(BOOT_LIMIT), "bash", str(script)],
                             cwd=HOME, stdin=subprocess.DEVNULL, stdout=f,
                             stderr=subprocess.STDOUT, env=BOOT_ENV, **AS_HER)
        took = round(time.monotonic() - began)
        timed_out = took >= BOOT_LIMIT            # the exit code alone cannot tell
        f.write(f"\n~/boot.sh {'stopped after the time limit' if timed_out else 'ended'}"
                f": exit {rc}, {took} s\n")
    STATUS["boot"].update(state="timed_out" if timed_out else "done", exit=rc, seconds=took)


# ---- files: the app's way in and out of its home -----------------------------------------

def her_path(rel: str) -> Path:
    """A path in its home from one relative to it (~/ and its home's own path allowed).
    Refuses one that leaves its home, also through a symlink."""
    rel = (rel or "").strip()
    for lead in (f"{HOME}/", "~/"):
        rel = rel.removeprefix(lead)
    if not rel or rel.endswith("/"):
        raise Problem("path is a file in the home folder, like uploads/game.exe.")
    p = Path(os.path.normpath(HOME / rel))
    if p == HOME or not p.resolve().is_relative_to(HOME.resolve()):
        raise Problem(f"{rel} is outside the home folder.")
    return p


def put_file(rel: str, body, length: int, origin: str = "") -> dict:
    """`length` bytes of `body` as its file at `rel`:
    to a temp name, then renamed, so a broken upload never leaves half a file. Its own (it
    writes it, write_hers), mode 0644, parent folders made. With `origin`, tagged with
    where it came from (ORIGIN)."""
    p = her_path(rel)
    free = shutil.disk_usage(HOME).free
    if length > free:
        raise Problem(f"The home disk has {free // 1048576} MB left and the file is "
                      f"{length // 1048576} MB.", 413)
    home_room(length)
    d = p.parent
    while not d.exists():
        d = d.parent
    if not d.is_dir():
        raise Problem(f"{d.relative_to(HOME)} is a file, not a folder.")
    if p.is_dir():
        raise Problem(f"{rel} is a folder.")
    tmp = p.with_name(f".{p.name}.part-{secrets.token_hex(3)}")
    try:
        write_hers(tmp, body, length)
        if origin:
            try:
                os.setxattr(tmp, ORIGIN, origin.encode()[:4000])
            except OSError:              # a home without user xattrs: the file still lands
                pass
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if (STATUS.get("home") or {}).get("limit"):
        STATUS["home"]["used"] += length        # until the next measure
    return {"path": str(p), "bytes": length}


def byte_range(header: str, size: int) -> tuple[int, int] | None:
    """The one range of `Range: bytes=a-b`, `a-` or `-n` as (first, last); None for no
    range, or several (the whole file then). ValueError when it starts past the end."""
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m or m.groups() == ("", ""):
        return None
    a, b = m.groups()
    first, last = (max(size - int(b), 0), size - 1) if a == "" else \
        (int(a), min(int(b), size - 1) if b else size - 1)
    if first > last:
        raise ValueError(header)
    return first, last


def splice(a: socket.socket, b: socket.socket) -> None:
    """Bytes both ways between two sockets until both sides are done; an end on one side
    is passed on to the other as its end of writing."""
    peer = {a: b, b: a}
    try:
        while peer:
            for s in select.select(list(peer), [], [])[0]:
                try:
                    data = s.recv(1 << 16)
                except OSError:
                    data = b""
                if data:
                    peer[s].sendall(data)
                    continue
                try:
                    peer.pop(s).shutdown(socket.SHUT_WR)
                except OSError:
                    pass
    except OSError:
        pass                     # one side went away for good: the tunnel is over


# ---- HTTP ------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def send_response(self, code, message=None):
        super().send_response(code, message)
        self.send_header("X-Shellbox-Reset", STATUS.get("reset") or "")

    def authorized(self) -> bool:
        scheme, _, key = self.headers.get("Authorization", "").partition(" ")
        if scheme == "Bearer" and hmac.compare_digest(
                hashlib.sha256(key.encode()).hexdigest(), KEY_HASH):
            return True
        self.send_json({"error": "Wrong or missing key."}, 401)
        return False

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":            # the health check: no key, nothing else
            return self.send_json({"ok": True})
        if not self.authorized():
            return
        if url.path == "/status":            # no forking here: works mid fork bomb
            return self.send_json(STATUS)
        if url.path == "/log":
            return self.log(parse_qs(url.query))
        if url.path == "/file":
            return self.get_file(parse_qs(url.query).get("path", [""])[0])
        if url.path == "/forward":
            return self.forward(parse_qs(url.query).get("port", [""])[0])
        if url.path.startswith("/code/"):
            return self.code(url.path, None)
        if url.path != "/view":
            return self.send_json({"error": "No such endpoint."}, 404)
        name = parse_qs(url.query).get("terminal", [""])[0]
        if not NAME.match(name) or not exists(name):
            return self.send_json({"error": f"No terminal named {name}."}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()

        def write(chunk: bytes) -> None:
            self.wfile.write(chunk)
            self.wfile.flush()
        view(name, self.connection, write)

    def do_POST(self):
        if not self.authorized():
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))
                              or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.send_json({"error": "The body is a JSON object."}, 400)
        calls = {"/type": type_call, "/stop": stop_call, "/read": read_call,
                 "/control": control_call, "/keys": keys_call}
        try:
            if self.path == "/run":
                return self.run(body, run_call)
            if self.path == "/wait":
                existing(check_name(body.get("terminal")), [])     # a 404 before the stream
                return self.run(body, wait_call)
            if self.path.startswith("/code/"):
                return self.code(self.path, body)
            if self.path not in calls:
                return self.send_json({"error": "No such endpoint."}, 404)
            try:
                res = calls[self.path](body)
            except (Problem, HungUp, BrokenPipeError, ConnectionResetError):
                raise
            except Exception as e:
                traceback.print_exc()
                res = {"state": "error", "error": f"{type(e).__name__}: {e}"}
            self.send_json(res)
        except Problem as e:
            self.send_json({"error": str(e), **e.extra}, e.status)
        except (HungUp, BrokenPipeError, ConnectionResetError):
            pass          # the app hung up; the terminal keeps going, like a closed laptop

    def do_PUT(self):
        if not self.authorized():
            return
        url = urlparse(self.path)
        if url.path != "/file":
            return self.send_json({"error": "No such endpoint."}, 404)
        length, q = self.headers.get("Content-Length", ""), parse_qs(url.query)
        if not length.isdigit():
            return self.send_json({"error": "A file upload needs its Content-Length."}, 411)
        try:
            res = put_file(q.get("path", [""])[0], self.rfile, int(length),
                           origin=q.get("origin", [""])[0])
        except Problem as e:
            self.close_connection = True      # the rest of the body is never read
            return self.send_json({"error": str(e)}, e.status)
        except OSError as e:
            self.close_connection = True
            return self.send_json({"error": f"The file could not be written: {e.strerror}."},
                                  500)
        self.send_json(res)

    def code(self, path: str, body: dict | None) -> None:
        """/code/*: the coding agent's sessions (CODE, shellbox/agents/). GET with body
        None, POST with the JSON body."""
        if CODE is None:
            said = (STATUS.get("code") or {}).get("error") or "no coding agent"
            return self.send_json({"error": f"The coding agent ({CODING_AGENT}) did not start "
                                            f"in the box: {said}. `sudo reboot` in a terminal "
                                            f"starts the box and it again."}, 503)
        parts = path.strip("/").split("/")          # code, session[, id[, action]]
        try:
            if parts[1:] == ["sessions"] and body is None:
                return self.send_json({"sessions": CODE.list()})
            if parts[1:] == ["session"] and body is not None:
                res = CODE.start(str(body.get("dir") or "."), str(body.get("task") or ""),
                                 str(body.get("model") or ""), body.get("session") or None,
                                 bool(body.get("websearch")))
                return self.send_json({**res, **watch_session(res["session"])})
            if len(parts) == 3 and parts[1] == "session" and body is None:
                return self.send_json(CODE.status(parts[2]))
            if len(parts) == 4 and parts[1:2] == ["session"] and parts[3] == "result" \
                    and body is None:
                return self.send_json(CODE.result(parts[2]))
            if len(parts) == 4 and parts[1:2] == ["session"] and parts[3] == "abort" \
                    and body is not None:
                return self.send_json(CODE.abort(parts[2]))
        except Exception as e:                      # the backend's Problem, or a surprise
            status = getattr(e, "status", None)
            if isinstance(status, int):
                return self.send_json({"error": str(e)}, status)
            traceback.print_exc()
            return self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)
        self.send_json({"error": "No such endpoint."}, 404)

    def get_file(self, rel: str) -> None:
        """The agent's file, streamed, or the one byte range the Range header asks for."""
        try:
            p = her_path(rel)
            if p.is_dir():
                return self.send_json({"error": f"{rel} is a folder.", "folder": True}, 400)
            f = p.open("rb")
        except Problem as e:
            return self.send_json({"error": str(e)}, e.status)
        except OSError:
            return self.send_json({"error": f"No file {rel} in the shellbox home."}, 404)
        with f:
            if not p.is_file():
                return self.send_json({"error": f"{rel} is not a file."}, 400)
            size = os.fstat(f.fileno()).st_size
            try:
                part = byte_range(self.headers.get("Range", ""), size)
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            first, last = part or (0, size - 1)
            self.send_response(206 if part else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            if part:
                self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
            self.send_header("Content-Length", str(last - first + 1))
            self.end_headers()
            f.seek(first)
            left = last - first + 1
            while left > 0 and (chunk := f.read(min(left, 1 << 20))):
                self.wfile.write(chunk)
                left -= len(chunk)

    def forward(self, port: str) -> None:
        """/forward: its server on that port in its box, raw both ways (splice)."""
        if not port.isdigit() or not 0 < int(port) < 65536:
            return self.send_json({"error": "port is a number from 1 to 65535."}, 400)
        try:
            up = socket.create_connection(("localhost", int(port)), timeout=FORWARD_WAIT)
        except OSError:
            return self.send_json({"error": f"Nothing is running on port {port}."}, 502)
        with up:
            up.settimeout(None)
            self.send_response(101)
            self.send_header("Upgrade", "tcp")
            self.send_header("Connection", "Upgrade")
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            splice(self.connection, up)

    def run(self, body: dict, fn) -> None:
        """/run and /wait: progress lines, then the result."""
        name = check_name(body.get("terminal") or "main")   # a 400 here, nothing typed
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            res = fn(body, self.send_line, self.connection)
        except (HungUp, BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:            # the stream has started: say it in the stream
            if not isinstance(e, Problem):
                traceback.print_exc()
            res = {"terminal": name, "state": "error", **getattr(e, "extra", {}),
                   "error": str(e) if isinstance(e, Problem) else f"{type(e).__name__}: {e}"}
        self.send_line(res)

    def log(self, query: dict) -> None:
        """A terminal's raw log from byte `from` on, at most LOG_CHUNK bytes."""
        tid, since = query.get("id", [""])[0], query.get("from", ["0"])[0]
        if not since.isdigit():
            return self.send_json({"error": "from is a byte offset."}, 400)
        got = read_whole(tid, int(since), LOG_CHUNK) if ID.match(tid) else None
        if got is None:
            return self.send_json({"error": f"No log with id {tid}."}, 404)
        data, size = got
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Log-Size", str(size))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data: dict, status: int = 200) -> None:
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_line(self, msg: dict) -> None:
        self.wfile.write((json.dumps(msg) + "\n").encode())
        self.wfile.flush()

    def log_message(self, *args):
        pass


def main() -> None:
    start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
