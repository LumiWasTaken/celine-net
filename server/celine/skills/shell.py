"""The agent's terminals: real ones, in its shellbox (shellbox/shellboxd.py).

Nine tools. shell__run runs a command in a terminal's shell and waits for its end (the
shellbox's run function writes its exit status, duration and log); asked into the
background, the wait becomes a job whose result comes back as a message. shell__type
types into a program like at a keyboard and shows the screen. shell__stop presses
Ctrl-C. shell__read pages a run's log, shows the screen, or lists the terminals.
shell__look shows its images of its home. shell__send attaches files of its home to the conversation (box_files.py streams it).
shell__share shows the user a server of its own or files of its home through the app
(shares.py); shell__unshare ends one, shell__list_shares lists them.

The shellbox answers with facts; every word it reads is composed here, next to the
guide (guides/shell.md), which quotes the numbers below: change both together.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlsplit

import anyio
import httpx

from ..core.ids import now, when
from ..terminals import store as terminals
from . import ToolContext, ToolResult, guide, tool, described
from ..clock import zone
from ..errors import CelineError, ToolRefused, bad_key, not_set_up, unreachable_words
from .. import shares, shellbox, vision
from ..settings import services as services_mod
from ..guard import sources

# ---- numbers --------------------------------------------------------------------------
RUN_MAX = 3000         # seconds one shell__run waits at most (the shellbox's RUN_MAX)
SELF_END = 3500        # the app ends a run the shellbox has not answered by then
TURN_MARGIN = 3        # seconds a run's wait ends before its turn's time: the answer
                       # comes back as "still running", not as the turn's "time is up"
HEAD, TAIL = 2000, 6000    # characters of a long output kept: its start and its end
READ_CHARS = 8000      # characters one shell__read page carries
PROBE_WAIT = 2.5       # seconds Settings, Features waits for the box's answer
WEDGE_MAX = 3          # wedges in one conversation, then shell__run and new terminals are
                       # refused there (the cap)

# ---- words that never change ------------------------------------------------------------
KEY_FORMAT = "keys is one string, names separated by spaces: keys=\"Escape\", keys=\"Down Down Enter\"."
KEY_NAMES = ("C-a to C-z (Ctrl), M-a (Alt), Escape, Enter, Tab, ShiftTab, Up, Down, Left, "
             "Right, Home, End, PageUp, PageDown, Backspace, Delete, Insert, Space, F1 to "
             "F12, or one single character")
NAME_RULE = ("A terminal name is up to 16 lowercase letters, digits, - or _, starting with a "
             "letter or digit, like terminal=\"build\".")
APT_LINE = "apt installs are gone after the reset. Put them in ~/boot.sh."
APT_INSTALL = re.compile(r"\bapt(?:-get)?\b.*\binstall\b")
APT_ERROR = re.compile(r"^E:", re.M)      # apt failed, whatever exit a pipe reports
FULL_LINE = ("A disk is full: `df -h ~ /tmp` shows which. Delete what you do not need; if your "
             "home needs more room, ask for it.")
SHARE_ASK = "make \"{title}\" a public share anyone with the link opens"
SHARE_WHY = "Anyone with the link opens it, not only {{username}}."
SHARE_NOBODY = "Nothing was shared: nobody is here to approve a public share."
NO_RUN_LINE = ("The shell there did not start it: shell__run needs bash with its run function "
               "(/etc/shellbox/run.sh, read by /etc/bash.bashrc). shell__type works in any "
               "shell.")

# ---- keys: what it may write (any case) -> tmux's name ----------------------------------
KEY_ALIASES = {
    "enter": "Enter", "return": "Enter", "cr": "Enter",
    "escape": "Escape", "esc": "Escape",
    "tab": "Tab", "shifttab": "BTab", "btab": "BTab",
    "backspace": "BSpace", "bspace": "BSpace", "bs": "BSpace",
    "delete": "DC", "del": "DC", "insert": "IC",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End",
    "pageup": "PPage", "pgup": "PPage", "ppage": "PPage",
    "pagedown": "NPage", "pgdn": "NPage", "npage": "NPage",
    "space": "Space",
    **{f"f{n}": f"F{n}" for n in range(1, 13)},
}
CTRL = re.compile(r"^(?:c-|ctrl[+-]|\^)(.)$", re.I)     # C-c, Ctrl+c, ctrl-c, ^C
ALT = re.compile(r"^(?:m-|alt[+-])(.)$", re.I)           # M-x, Alt+x
# a text that is only a key written out: it meant keys (llama.cpp hands strings over raw)
KEY_AS_TEXT = re.compile(r"^(?:(?:c-|ctrl[+-]|\^|m-|alt[+-]).|\\u[0-9a-f]{4}|\\x[0-9a-f]{2}"
                         r"|\\[nrte]|\\0?33|enter|return|escape|esc|tab|shifttab|backspace"
                         r"|delete|space|up|down|left|right|pageup|pagedown|f[1-9]|f1[0-2])$",
                         re.I)
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")    # the shellbox's NAME
log = logging.getLogger("celine.skills.shell")


def key_name(word: str) -> str | None:
    """One key as it wrote it -> tmux's name, or None when it is not a key."""
    if len(word) == 1:
        return word
    if word.lower() in KEY_ALIASES:
        return KEY_ALIASES[word.lower()]
    m = CTRL.match(word)
    if m and (m[1].isascii() and m[1].isalpha() or m[1] in "\\]"):
        return f"C-{m[1].lower()}"
    m = ALT.match(word)
    if m:
        return f"M-{m[1]}"
    return None


def parse_keys(value: Any) -> list[str]:
    """keys -> tmux names, all checked before anything is sent. A JSON array of names
    (a habit of some models) counts as the names joined with spaces."""
    if isinstance(value, list):
        words = " ".join(str(v) for v in value).split()
    else:
        text = str(value or "").strip()
        if text.startswith("["):
            try:
                listed = json.loads(text)
            except ValueError:
                listed = None
            if isinstance(listed, list) and all(isinstance(v, str) for v in listed):
                text = " ".join(listed)
        words = text.split()
    names = []
    for word in words:
        name = key_name(word)
        if name is None:
            raise ToolRefused(f"Unknown key {json.dumps(word)}. {KEY_FORMAT} The names: "
                          f"{KEY_NAMES}.")
        names.append(name)
    return names


def check_text(text: str) -> None:
    """A text that is only a key notation (C-c, ^C, \\u0003, Escape, Enter) is refused."""
    t = text.strip()
    if (len(t) == 1 and (ord(t) < 32 or t == "\x7f")) or KEY_AS_TEXT.match(t):
        raise ToolRefused(f"text {json.dumps(t)} is a key, and text is typed as written. Special "
                      f"keys go in keys, by name: keys=\"Escape\", keys=\"Enter\"; Ctrl-C is "
                      f"shell__stop.")


def check_name(name: str) -> str:
    if not NAME.match(name):
        raise ToolRefused(NAME_RULE)
    return name


def as_number(value: Any, what: str, example: str) -> float | None:
    """A number, also written as a string ("30"); None stays None."""
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            raise ValueError
        return float(value)
    except (TypeError, ValueError):
        raise ToolRefused(f"{what} is a number, like {example}.") from None


def as_bool(value: Any, what: str) -> bool:
    """true or false, also written as a string: bool("false") would be True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ToolRefused(f"{what} is true or false, like {what}=false.")


# ---- facts -> words -----------------------------------------------------------------------
WRAPPERS = {"sudo", "env", "nohup", "time"}
VAR = re.compile(r"^\w+=")


def program_of(res: dict) -> tuple[str | None, int | None]:
    """What runs in front, by name and pid. Wrappers (sudo, env, nohup, time, VAR=value)
    are skipped; when that leaves nothing (sudo alone, asking for a password) the first
    name stays; of several, the lowest pid."""
    front = [{**p, "name": shown_name(p)} for p in res.get("front") or []
             if isinstance(p, dict) and p.get("name")]
    if not front:
        return res.get("program"), res.get("pid")
    real = [p for p in front if p["name"] not in WRAPPERS and not VAR.match(p["name"])]
    chosen = (real or front)[0]
    return chosen["name"], chosen.get("pid")


def shown_name(p: dict) -> str:
    """The kernel's name of a process, or its program's when no argument holds that name:
    node 24 names itself MainThread."""
    args = [str(a) for a in p.get("args") or []]
    if args and not any(p["name"] in a for a in args):
        return os.path.basename(args[0]).lstrip("-") or p["name"]
    return p["name"]


def args_of(res: dict, pid: int | None) -> list[str] | None:
    for p in res.get("front") or []:
        if isinstance(p, dict) and p.get("pid") == pid:
            return list(p.get("args") or [])
    return None


def dur(seconds: Any, exact: bool = False) -> str:
    """3.1 s (exact, under 10 s), 48 s, 14 min 2 s, 2 h 5 min."""
    s = float(seconds or 0)
    if exact and s < 10:
        return f"{s:.1f} s"
    s = int(round(s))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60} s"
    return f"{s // 3600} h {s % 3600 // 60} min"


def last_line(text: str) -> str:
    return next((ln for ln in reversed((text or "").splitlines()) if ln.strip()), "")


def first_line(text: str) -> str:
    return next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")[:200]


def opened_words(opener: dict | None, here: str) -> str:
    """Which conversation one of its terminals belongs to, for its listing."""
    if opener is None:
        return ""
    if opener["id"] == here:
        return ", opened here"
    kind = {"mission": "mission", "sub_agent": "sub-agent"}.get(opener["kind"], "conversation")
    title = opener["title"]
    return f", opened in {kind} {json.dumps(title, ensure_ascii=False)}" if title else \
        f", opened in another {kind}"


def call(tool_name: str, terminal: str, **args) -> str:
    """A call as it would write it: shell__read(terminal="main", from=812)."""
    shown = ", ".join([f'terminal="{terminal}"'] +
                      [f"{k}={json.dumps(v)}" for k, v in args.items()])
    return f"{tool_name}({shown})"


# per-program hints, on waiting and full-screen states; {name} is the program's name
KIND = {
    "vim": "vim", "vi": "vim", "vim.basic": "vim", "vim.tiny": "vim", "nvim": "vim",
    "nano": "nano", "editor": "editor", "sensible-editor": "editor",
    "less": "pager", "more": "pager", "man": "pager", "pager": "pager",
    "htop": "top", "top": "top",
    "python3": "python", "python": "python", "node": "node", "nodejs": "node",
    "sqlite3": "sqlite3", "psql": "psql", "mysql": "mysql", "mariadb": "mysql",
    "ssh": "ssh", "cat": "cat", "sudo": "sudo",
    "bash": "shell", "sh": "shell", "dash": "shell", "zsh": "shell", "fish": "shell",
    "nu": "shell", "su": "shell",
    "apt": "apt", "apt-get": "apt", "aptitude": "apt", "dpkg": "dpkg",
}
HINTS = {
    "vim": "New line below the cursor: keys=\"o\", your text with enter=false, then "
           "keys=\"Escape\". Save and leave: keys=\"Escape\", text=\":wq\". Leave without "
           "saving: keys=\"Escape\", text=\":q!\".",
    "nano": "nano waits for keys. Leave: keys=\"C-x\", then text=\"y\" saves (Enter confirms "
            "the file name) or text=\"n\" drops the changes.",
    "editor": "{name} is your editor, nano or vim. nano (GNU nano at the top): keys=\"C-x\", "
              "then text=\"y\" saves or text=\"n\" drops. vim: keys=\"Escape\", text=\":wq\" "
              "saves, text=\":q!\" drops.",
    "pager": "{name} shows a page: keys=\"Space\" scrolls, text=\"/word\" searches, text=\"q\" "
             "with enter=false leaves.",
    "top": "{name} shows a live screen: text=\"q\" with enter=false leaves.",
    "python": "{name} waits at its >>> prompt: type Python with shell__type, or "
              "text=\"exit()\" leaves.",
    "node": "{name} waits at its > prompt: type JavaScript with shell__type, or "
            "text=\".exit\" leaves.",
    "sqlite3": "sqlite3 waits for SQL, ended with ;. text=\".quit\" leaves.",
    "psql": "psql waits for SQL, ended with ;. text=\"\\q\" leaves.",
    "mysql": "{name} waits for SQL, ended with ;. text=\"exit\" leaves.",
    "ssh": "ssh: you are on another machine; type there with shell__type. When the last "
           "line is the remote prompt, your command there finished. text=\"exit\" comes back.",
    "cat": "cat reads what you type until keys=\"C-d\"; shell__stop stops it.",
    "sudo": "sudo asks for a password, but yours needs none here: shell__stop, and check "
            "the command.",
    "shell": "A second shell ({name}) runs inside this one: type into it with shell__type, "
             "text=\"exit\" comes back.",
    "apt": "{name} asks something: read the last lines and answer with shell__type. Next "
           "time pass -y.",
    "apt_yes_no": "It asks a yes or no question: shell__type with text=\"y\". Next time pass -y.",
    "dpkg": "dpkg asks about a config file: text=\"N\" keeps yours, text=\"Y\" takes the new "
            "one.",
    "dialog": "A setup dialog asks something: the arrow keys choose, keys=\"Tab\" moves to "
              "the buttons, keys=\"Enter\" picks.",
    "yes_no": "It asks a yes or no question: shell__type with text=\"y\" or text=\"n\".",
}
REPLS = ("python", "node")       # a hint only without a script: python3 -m http.server is a server
YES_NO = re.compile(r"\[(?:y/n|yes/no)\]|\((?:y/n|yes/no)\)", re.I)


def hint(name: str | None, args: list[str] | None, state: str, last: str) -> str | None:
    """The one line that tells it how to deal with the program in front."""
    kind = KIND.get(name or "")
    if kind == "apt":
        if state == "fullscreen":
            return HINTS["dialog"]
        return HINTS["apt_yes_no"] if YES_NO.search(last) else HINTS["apt"].format(name=name)
    if kind == "dpkg" and state == "fullscreen":
        return HINTS["dialog"]
    if kind in REPLS and (args is None or len(args) > 1):
        kind = None
    if kind:
        return HINTS[kind].format(name=name)
    if YES_NO.search(last):
        return HINTS["yes_no"]
    return None


def word_note(code: str) -> str:
    """The shellbox's note codes, in words. A code this app does not know is shown as it
    is, never a crash."""
    key, _, arg = str(code).partition(":")
    if key == "login_shell_missing":
        return (f"Your login shell {arg} is not there (after a reset ~/boot.sh may still be "
                f"installing it), so this terminal runs bash.")
    if key == "tmux_gone":
        return ("Every terminal ended, but the shellbox kept running: something killed them "
                "(kill, out of memory), not the reset.")
    if key == "made_room":
        return (f"The terminals were at their limit, so {arg}, the one of yours that waited "
                f"at its prompt quiet the longest, was closed to open this one.")
    if key == "pipe_rearmed":
        return "Some output of this terminal was not recorded in its history."
    if key == "home_over":
        used, _, limit = arg.partition(":")
        try:
            used_gb, limit_gb = int(used) / 1024 ** 3, int(limit) / 1024 ** 3
        except ValueError:
            return f"The shellbox noted: {code}."
        return (f"Your home holds {used_gb:.1f} GB, past its {limit_gb:g} GB. Delete what you "
                f"do not need (`du -sh ~/* ~/.[!.]*` shows where it is); if you need the "
                f"room, ask for more.")
    return f"The shellbox noted: {code}."


def boot_note(res: dict) -> str:
    """The reset or restart sentence, with how ~/boot.sh went."""
    started = str(res.get("started") or str(res.get("boot_id") or "").split("+")[0])
    try:
        t = datetime.fromisoformat(started)
        when = f" at {t:%H:%M}" if t.date() == datetime.now().date() \
            else f" on {t:%Y-%m-%d} at {t:%H:%M}"
    except ValueError:
        when = ""
    if res.get("fresh"):
        head = f"Your shellbox was reset{when}: fresh Debian, your home kept."
    else:
        head = (f"Your shellbox restarted{when}: your terminals and what ran in them ended; "
                f"the files are all still there.")
    boot = res.get("boot") or {}
    state = boot.get("state")
    if state == "done":
        return (f"{head} ~/boot.sh ran: exit {boot.get('exit')} in "
                f"{dur(boot.get('seconds'))} (log: ~/boot.log).")
    if state == "running":
        return f"{head} ~/boot.sh is still running (log: ~/boot.log)."
    if state == "timed_out":
        return (f"{head} ~/boot.sh was stopped at its time limit after "
                f"{dur(boot.get('seconds'))} (log: ~/boot.log).")
    if state == "failed":
        return f"{head} ~/boot.sh could not run: {boot.get('error') or 'no reason given'}."
    if state == "none":
        return f"{head} You have no ~/boot.sh, so nothing was set up again."
    return head


def cut_marker(res: dict, name: str) -> str:
    """What the one cut left out, numbered as sed numbers the log, and how to read it."""
    cut = res["cut"]
    first, last, total = int(cut["first"]), int(cut["last"]), int(cut["total"])
    log = res.get("log") or ""
    return (f"[lines {first} to {last} of {total} are not shown. "
            f"{call('shell__read', name, **{'from': int(cut['from'])})} reads on from line "
            f"{first}; sed -n '{first},{first + 79}p' {log} works too.]")


SEND_MAX = 5           # files one shell__send attaches at most
LOOK_MAX = 4           # images one shell__look shows at most
LOOKS = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
         "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp"}


def path_list(value: Any) -> list[str] | str:
    """One path or a list of them, each a file (not ending in /); a sentence when not."""
    paths = [str(p).strip() for p in (value if isinstance(value, list) else [value])
             if str(p or "").strip()]
    if not paths or any(p.endswith("/") for p in paths):
        return "path is a file in your home, like path=\"~/out/report.md\"."
    return paths


# a shell__run it asked into the background: its result while the job has it ("head" 1:
# all of it its own, the commands log shows none of it)
SHELL_DETACHED = ("running in the background as job {job}, watching terminal {terminal}. "
                  "You'll get the result as a message when the watch ends.")


class Shell:
    name = "shell"
    exact_args = True   # turns/tools.py: in a terminal "a" is not "A"
    app = None          # the running app (core/services.py), from skills.build
    guide = guide("shell")
    description = described()

    def __init__(self, cfg, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.transport = transport                  # tests: a fake shellbox
        # per conversation: the boot id it last saw, the apt note and the full-disk note,
        # the programs it had a hint for, and the runs whose end it was told
        self.boots: dict[str, str] = {}
        self.apt_noted: set[str] = set()
        self.full_noted: set[str] = set()
        self.hinted: set[tuple[str, str]] = set()
        self.told: set[tuple[str, str]] = set()
        # per conversation: its wedges so far (in memory: an app restart starts at 0)
        self.wedges: dict[str, int] = {}

    def carry(self, old) -> None:
        """A reload's rebuild keeps what it was already told (reload.py)."""
        for a in ("boots", "apt_noted", "full_noted", "hinted", "told", "wedges"):
            setattr(self, a, getattr(old, a))

    @property
    def status(self) -> str:
        return "up" if shellbox.url(self.cfg) else "down"

    @property
    def error(self) -> str | None:
        if not shellbox.url(self.cfg):
            return not_set_up("the shellbox")
        return None

    # ---- the tools ------------------------------------------------------------------------

    @tool(mode="detach", read_only=False, destructive=True, open_world=True,
          background="background", timeout=SELF_END, terminal_arg="terminal",
          detached=SHELL_DETACHED, detached_display={"head": 1})
    async def run(
        self,
        ctx: ToolContext,
        command: Annotated[str, "The command line, run by your shell as if typed at its "
                                "prompt. Several lines are fine."],
        timeout: Annotated[float | None, "Most seconds to wait, 1 to 3000. Without it, until "
                                         "the command ends or the time of your turn is up. "
                                         "The command keeps running after it."] = None,
        terminal: Annotated[str | None, "Which terminal. Leave it out for your default "
                                        "terminal; a new name opens a new terminal."] = None,
        background: Annotated[bool, "True hands the wait to a background job: the call "
                                    "answers within 5 seconds and the result reaches you as a "
                                    "message when the command ends."] = False,
    ) -> ToolResult:
        """Run a command in your shell, in a terminal of your shellbox, and wait for its
        end. The shell keeps its directory and variables between runs. For programs that
        wait for keys (ssh, python3, vim, a server's console) use shell__type.
        Returns: a status line (terminal, directory, exit status and duration, or still
        running), the output (its start and end when long) and the log's path."""
        try:
            name = check_name(str(terminal or "main"))
            command = "" if command is None else str(command)
            if not command.strip():
                raise ToolRefused("command is the command line to run, like command=\"ls -la\".")
            given = as_number(timeout, "timeout", "timeout=60")
            seconds = float(RUN_MAX) if given is None else min(max(given, 1), RUN_MAX)
            if ctx.time_left is not None and not as_bool(background, "background"):
                seconds = max(1, min(seconds, int(ctx.time_left - TURN_MARGIN)))
        except ToolRefused as e:
            return self._refused(str(e), terminal)
        if self._capped(ctx):
            return self._refused(self._cap_words(), name)
        if (refused := await self._not_hers(ctx, name)) is not None:
            return refused
        body = {"terminal": name, "command": command, "timeout": seconds,
                "chars": self._chars(ctx, HEAD + TAIL)}

        async def progress(line: str) -> None:
            if ctx.emit is not None:
                try:
                    await ctx.emit(line[:200])
                except Exception:       # a lost progress line is no error, but leaves a trace
                    log.debug("progress line lost", exc_info=True)

        try:
            with anyio.move_on_after(SELF_END) as limit:
                res = await self._run_request(body, progress)
            if limit.cancelled_caught:
                return ToolResult(content=f"The shellbox did not answer in {dur(SELF_END)}; "
                                          f"the command may still run. "
                                          f"{call('shell__read', name)} shows the terminal.",
                                  is_error=True, display={"terminal": name})
        except ToolRefused as e:
            return self._refused(str(e), name)
        except httpx.HTTPError as e:
            return self._unreachable(name, e)
        return await self._result(ctx, res, "run", name, text=command)

    @tool(read_only=False, destructive=True, open_world=True, terminal_arg="terminal")
    async def type(
        self,
        ctx: ToolContext,
        text: Annotated[str, "Typed exactly as written. Several lines arrive as one paste. "
                             "Use real line breaks: a written \\n arrives as a backslash and "
                             "an n."] = "",
        keys: Annotated[str, "Key names pressed before text, separated by spaces: Escape, "
                             "Enter, Tab, Up, Down, Left, Right, Home, End, PageUp, PageDown, "
                             "Backspace, Delete, Space, F1 to F12, C-d (Ctrl), or one single "
                             "character. Examples: \"Escape\", \"Down Down Enter\"."] = "",
        terminal: Annotated[str | None, "Which terminal. Leave it out for your default "
                                        "terminal."] = None,
        enter: Annotated[bool, "Press Enter after text. False types without Enter, for a "
                               "single letter in a full-screen program."] = True,
    ) -> ToolResult:
        """Type into the program in front in a terminal, like at a keyboard: ssh, python3,
        vim, a question a command asks. Enter is pressed after your text; special keys go in
        keys. It answers with the screen once the terminal printed nothing for a moment;
        it never waits for a program to end (that is shell__run).
        Returns: a status line (terminal, what is in front and whether it waits for input),
        then the screen."""
        try:
            name = check_name(str(terminal or "main"))
            text = "" if text is None else str(text)
            key_list = parse_keys(keys)
            if text:
                check_text(text)
            press = as_bool(enter, "enter")
        except ToolRefused as e:
            return self._refused(f"{e} Nothing was typed.", terminal)
        if self._capped(ctx):
            try:
                status, listed = await self._post("/read", {}, read=60)
            except (ToolRefused, httpx.HTTPError):
                listed = {}
            if name not in {t.get("name") for t in listed.get("terminals") or []
                            if isinstance(t, dict)}:
                return self._refused(f"{self._cap_words()} Nothing was typed.", name)
        if (refused := await self._not_hers(ctx, name)) is not None:
            return refused
        notes = []
        if text.endswith("\\n"):
            notes.append("Your text ended in a written \\n: it arrived as a backslash and an "
                         "n. Enter is pressed for you; real line breaks work too.")
        body = {"terminal": name, "text": text, "keys": key_list, "enter": bool(press and text)}
        try:
            status, res = await self._post("/type", body, read=60)
        except ToolRefused as e:
            return self._refused(f"{e} Nothing was typed.", name)
        except httpx.HTTPError as e:
            return self._unreachable(name, e)
        return await self._result(ctx, res, "type", name, text=text, keys=key_list,
                                  notes=notes)

    @tool(read_only=False, destructive=True, open_world=True, terminal_arg="terminal")
    async def stop(
        self,
        ctx: ToolContext,
        terminal: Annotated[str | None, "Which terminal. Leave it out for your default "
                                        "terminal."] = None,
    ) -> ToolResult:
        """Press Ctrl-C in a terminal: the program in front stops, or the run waiting
        there ends (usually exit 130). A program that reads Ctrl-C as a key (python3, nano)
        outlives it; the next shell__stop ends it. Nothing is ever stopped for you without
        this call.
        Returns: how the run ended, or the screen."""
        try:
            name = check_name(str(terminal or "main"))
        except ToolRefused as e:
            return self._refused(str(e), terminal)
        if (refused := await self._not_hers(ctx, name)) is not None:
            return refused
        try:
            status, res = await self._post("/stop", {"terminal": name,
                                                     "chars": self._chars(ctx, HEAD + TAIL)},
                                           read=60)
        except ToolRefused as e:
            return self._refused(str(e), name)
        except httpx.HTTPError as e:
            return self._unreachable(name, e)
        if status == 404:
            return self._unknown(name, res)
        return await self._result(ctx, res, "stop", name, keys=["C-c"])

    @tool(read_only=True, open_world=True)
    async def read(
        self,
        ctx: ToolContext,
        terminal: Annotated[str | None, "Which terminal. Leave it out to list them all."] = None,
        from_: Annotated[int | None, "A byte position in the log of the terminal's latest run: "
                                     "its output from there on, a page at a time. Results "
                                     "name the position to read on from. Leave it out for "
                                     "the screen."] = None,
    ) -> ToolResult:
        """Read a terminal without typing: the log of its latest run from a byte position
        (a page, with a more: line naming where the next starts), or the screen, always for
        a full-screen program. Without terminal it lists your terminals and {{username}}'s.
        It changes nothing.
        Returns: a status line, then the page or the screen, or one line per terminal."""
        try:
            name = check_name(str(terminal)) if terminal else None
            since = as_number(from_, "from", "from=0")
            if since is not None and since < 0:
                raise ToolRefused("from is a byte position, 0 or more.")
        except ToolRefused as e:
            return self._refused(str(e), terminal)
        body: dict[str, Any] = {"chars": self._chars(ctx, READ_CHARS)}
        if name:
            body["terminal"] = name
        if since is not None:
            body["from"] = int(since)
        try:
            status, res = await self._post("/read", body, read=60)
        except ToolRefused as e:
            return self._refused(str(e), name)
        except httpx.HTTPError as e:
            return self._unreachable(name, e)
        if status == 404:
            return self._unknown(name, res)
        if name is None:
            return await self._listing(ctx, res)
        return await self._result(ctx, res, "read", name)

    @tool(read_only=False, destructive=False, open_world=True)
    async def send(
        self,
        ctx: ToolContext,
        path: Annotated[str | list[str], "A file in your home, like \"~/out/report.md\", or "
                                         "a list of up to 5."],
    ) -> ToolResult:
        """Send {{username}} files from your shellbox: they are attached to this
        conversation, images shown, other files to download. Up to 5 at once; for more,
        zip them first (`zip -r ~/out/files.zip <folder>`) and send the zip.
        Returns: each file's name and size."""
        from .. import attachments, box_files
        paths = path_list(path)
        if isinstance(paths, str):
            return ToolResult(content=paths, is_error=True)
        if len(paths) > SEND_MAX:
            return ToolResult(content=f"That is {len(paths)} files; send at most {SEND_MAX} "
                                      f"at once. Zip them first (zip -r ~/out/files.zip "
                                      f"<folder>) and send the zip.", is_error=True)
        if ctx.store is None:
            return ToolResult(content="Only a conversation can take a file.", is_error=True)
        lines, sent = [], []
        for rel in paths:
            name = rel.rsplit("/", 1)[-1]
            try:
                row = await attachments.save_stream(
                    self.cfg, ctx.store, box_files.stream(self.cfg, rel, transport=self.transport),
                    name, None, tool_call_id=ctx.tool_call_id)
            except (FileNotFoundError, RuntimeError) as e:
                lines.append(str(e))
                continue
            except httpx.HTTPError as e:
                return self._unreachable(None, e)
            sent.append({k: row[k] for k in ("id", "sha256", "filename", "media_type",
                                             "bytes")})
            lines.append(f"Sent {self.cfg.user} {name} ({attachments.size(row['bytes'])}).")
        return ToolResult(content="\n".join(lines), is_error=not sent,
                          display={"paths": paths, "attachments": sent})

    @tool(read_only=True, open_world=True)
    async def look(
        self,
        ctx: ToolContext,
        path: Annotated[str | list[str], "An image in your home (png, jpg, webp, gif, bmp), "
                                         "like \"~/shots/screen.png\", or a list of up to 4."],
        question: Annotated[str | None, "What you want to know about it, when your model "
                                        "cannot see images and another describes it."] = None,
    ) -> ToolResult:
        """Look at images from your shellbox: a screenshot, a plot, a picture you made.
        Returns: the images, or their descriptions when your model cannot see images."""
        from .. import attachments, box_files
        paths = path_list(path)
        if isinstance(paths, str):
            return ToolResult(content=paths, is_error=True)
        if len(paths) > LOOK_MAX:
            return ToolResult(content=f"Look at most {LOOK_MAX} images at once.",
                              is_error=True)
        if ctx.store is None:
            return ToolResult(content="Only a conversation can look at images.",
                              is_error=True)
        sees = await attachments.turn_sees_images(self.cfg, ctx)
        lines, ids = [], []
        for rel in paths:
            name = rel.rsplit("/", 1)[-1]
            media = LOOKS.get(name.rsplit(".", 1)[-1].lower())
            if media is None:
                lines.append(f"{name} is not a png, jpg, webp, gif or bmp image.")
                continue
            try:
                row = await attachments.save_stream(
                    self.cfg, ctx.store, box_files.stream(self.cfg, rel, transport=self.transport),
                    name, media, tool_call_id=ctx.tool_call_id, in_message=False,
                    limit=int(self.cfg.r("max_attachment_bytes")))
            except (FileNotFoundError, RuntimeError) as e:
                lines.append(str(e))
                continue
            except CelineError as e:
                lines.append(e.message)
                continue
            except httpx.HTTPError as e:
                return self._unreachable(None, e)
            ids.append(row["id"])
            if sees:
                lines.append(f"{name} ({attachments.size(row['bytes'])}):")
            else:
                data = attachments.blob_path(self.cfg, row["sha256"]).read_bytes()
                llm = self.app.llm if self.app is not None else None
                said = await vision.describe(llm, data, ctx.conversation_id, ctx.turn_id,
                                             question)
                lines.append(f"{name}: {said}")
        return ToolResult(content="\n".join(lines), is_error=not ids,
                          display={"paths": paths},
                          images=ids if sees and ids else None)

    @tool(read_only=False, destructive=False, open_world=True)
    async def share(
        self,
        ctx: ToolContext,
        title: Annotated[str, "What it is, like \"Snake\" or \"Weather dashboard\"."],
        port: Annotated[int | None, "A port a server of yours listens on in your box, like "
                                    "8000."] = None,
        path: Annotated[str | None, "Or a file or folder in your home, like "
                                    "\"~/out/clip.mp4\" or \"~/snake/dist\"."] = None,
        public: Annotated[bool, "False (the default): private, only {{username}} opens it, "
                                "logged in to the app. True: anyone with the link opens it; "
                                "only when {{username}} explicitly asks for a link others "
                                "can open, and it waits for their approval first."] = False,
        hours: Annotated[float | None, f"How long it runs: by default {shares.PRIVATE_HOURS} "
                                       f"hours, at most {shares.PRIVATE_MAX}; a public one "
                                       f"{shares.PUBLIC_HOURS} hours, at most "
                                       f"{shares.PUBLIC_MAX}."] = None,
    ) -> ToolResult:
        """Show {{username}} something in your shellbox: a server you run on a port (a web
        app, a game, a dashboard) or a file or folder in your home (a video, an image, a
        page, a built game). They see it as a card under your reply and open it in a tab of
        its own. A share is private; a public one waits for their approval. Returns: its
        id, its link and when it ends."""
        title = (title or "").strip()
        if (port is None) == (path is None or not str(path).strip()):
            return ToolResult(content="Give a port or a path, one of them.", is_error=True)
        if not title or len(title) > shares.TITLE_MAX:
            return ToolResult(content=f"title is a name of 1 to {shares.TITLE_MAX} "
                                      f"characters.", is_error=True)
        if port is not None and not 0 < port < 65536:
            return ToolResult(content="port is a number from 1 to 65535.", is_error=True)
        most = shares.PUBLIC_MAX if public else shares.PRIVATE_MAX
        if hours is not None and not 0 < hours <= most:
            return ToolResult(content=f"hours is more than 0 and at most {most} for a "
                                      f"{'public' if public else 'private'} share.",
                              is_error=True)
        rel = str(path).strip().rstrip("/") if port is None else None
        what = f"port {port}" if port is not None else shellbox.home_short(rel)
        if public and (no := await self._public(ctx, title, what, hours)):
            return ToolResult(content=no, is_error=True)
        try:
            row = await shares.create(self.cfg, ctx.store, title, port=port, path=rel,
                                      conversation_id=ctx.conversation_id, public=public,
                                      hours=hours)
            st = await shares.state(self.cfg, ctx.store, row)
        except RuntimeError as e:
            return ToolResult(content=str(e), is_error=True)
        except httpx.HTTPError as e:
            return self._unreachable(None, e)
        who = "Anyone with the link opens it" if public else \
            f"Only {self.cfg.user} opens it, logged in to the app"
        lines = [f"Shared \"{title}\" ({what}) as share {row['token']}. {self.cfg.user} sees it "
                 f"as a card under your reply. {who}: {shares.link(self.cfg, row)}",
                 f"Your own browser opens it at {shares.INSIDE}{shares.page_path(row)}",
                 f"It ends {when(row['ends_at'], zone(self.cfg))}, at shell__unshare "
                 f"or at the next reset of your box."]
        if st == "down":
            lines.append(f"Nothing is running on port {port} yet: it shows once your "
                         f"server answers there.")
        return ToolResult(content="\n".join(lines),
                          display={"share": {"token": row["token"], "title": title}})

    async def _public(self, ctx: ToolContext, title: str, what: str,
                      hours: float | None) -> str | None:
        """The user's approval of a public share (celine/approvals.py, kind `share`),
        guard or no guard: None when approved, else the sentence for its result. Its
        words are fixed and the call's own data."""
        approvals = getattr(self.app, "approvals", None)
        if approvals is None:
            return SHARE_NOBODY
        h = shares.PUBLIC_HOURS if hours is None else hours
        action = SHARE_ASK.format(title=title)
        return await approvals.ask(
            action, [what, f"for {h:g} hour" + ("" if h == 1 else "s")],
            ctx.conversation_id, self.cfg.substitute(SHARE_WHY), action, kind="share",
            call=ctx.tool_call_id or None)

    @tool(read_only=False, destructive=False, open_world=True)
    async def unshare(
        self,
        ctx: ToolContext,
        id: Annotated[str, "The share's id, from shell__share or shell__list_shares."],
    ) -> ToolResult:
        """End a share: its link stops working at once."""
        if await shares.end(ctx.store, id.strip()):
            return ToolResult(content=f"Ended share {id.strip()}.")
        return ToolResult(content=f"No running share {id.strip()}; shell__list_shares lists them.",
                          is_error=True)

    @tool(read_only=True, open_world=True)
    async def list_shares(self, ctx: ToolContext) -> ToolResult:
        """List your shares that run, and those that ended in the last day: id, title, what
        it shows, whether it answers, public or not, when it ends or ended."""
        rows = await shares.listing(ctx.store, since=shares.clock() - shares.ENDED_SHOWN)
        states = [await shares.state(self.cfg, ctx.store, r) for r in rows]
        tz = zone(self.cfg)
        lines = [f"- {r['token']}: \"{r['title']}\", "
                 + (f"port {r['port']}" if r["port"] is not None
                    else shellbox.home_short(r["path"]))
                 + (", public" if r["public"] else "")
                 + (f", ended {when(shares.end_of(r), tz)}" if st == "ended" else
                    (", answers" if st == "live" else ", nothing answers")
                    + f", ends {when(r['ends_at'], tz)}")
                 for r, st in zip(rows, states)]
        return ToolResult(content="\n".join(lines) or "No shares.",
                          display={"shares": len(lines)})

    # ---- the shellbox over HTTP ------------------------------------------------------------

    async def _run_request(self, body: dict, progress) -> dict:
        """POST /run: progress lines go to `progress`, the final object comes back. A
        stream that ends without one is a dropped connection."""
        if not shellbox.url(self.cfg):
            raise ToolRefused(not_set_up("the shellbox"))
        async with shellbox.client(self.cfg, None, self.transport) as client, \
                client.stream("POST", shellbox.url(self.cfg) + "/run", json=body) as r:
            if r.status_code != 200:
                await r.aread()
                raise shellbox.refusal(self.cfg, r, self.cfg.user)
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    raise httpx.ReadError("not a line of JSON") from None
                if "progress" in msg and "state" not in msg:
                    await progress(str(msg["progress"]))
                    continue
                return msg
        raise httpx.ReadError("the stream ended without a result")

    async def _post(self, path: str, body: dict, read: float) -> tuple[int, dict]:
        return await shellbox.call(self.cfg, "POST", path, body, read=read, transport=self.transport,
                                   user=self.cfg.user)

    @staticmethod
    def _chars(ctx: ToolContext, most: int) -> int:
        """The most characters of output one result may carry: the shellbox cuts once."""
        if ctx.max_output_tokens:
            return max(200, min(most, int(ctx.max_output_tokens) * 3))
        return most

    @staticmethod
    def _refused(text: str, terminal) -> ToolResult:
        return ToolResult(content=text, is_error=True,
                          display={"terminal": str(terminal) if terminal else None,
                                   "refused": True})

    async def _not_hers(self, ctx: ToolContext, name: str) -> ToolResult | None:
        """The user's own terminal: its own to read, to touch only while they allow it."""
        if ctx.store is None:
            return None
        row = await terminals.open_by_name(ctx.store, name)
        if row and row["owner"] != "celine" and not row["may_type"]:
            user = self.cfg.user
            return self._refused(f"{name} is {user}'s terminal. You may read it, not type or "
                                 f"run in it. Ask {user}, or use your own terminal.", name)
        return None

    @staticmethod
    def _unknown(name: str | None, res: dict) -> ToolResult:
        names = [str(t) for t in res.get("terminals") or []]
        content = (f"No terminal named {name}. Your terminals: {', '.join(names)}."
                   if names else
                   f"No terminal named {name}. You have no terminals open; shell__run opens "
                   f"one.")
        return ToolResult(content=content, display={"terminal": name, "unknown": True})

    def _unreachable(self, name: str | None, e: httpx.HTTPError) -> ToolResult:
        """No answer from the box: which way (refused, timed out, cut off), as one
        sentence that names its address."""
        if isinstance(e, (httpx.ConnectError, httpx.TimeoutException,
                          httpx.UnsupportedProtocol)):
            content = unreachable_words("the shellbox", shellbox.url(self.cfg), e)
        else:
            what = f"terminal {name} and what ran in it are" if name else \
                "your terminals and what ran in them are"
            content = (f"The shellbox at {urlsplit(shellbox.url(self.cfg)).netloc} dropped the connection "
                       f"or restarted. If it was reset, {what} gone. Look with shell__read.")
        return ToolResult(content=content, is_error=True,
                          display={"terminal": name, "unreachable": True})

    # ---- composing its result --------------------------------------------------------------

    def _notes(self, ctx: ToolContext, res: dict, name: str | None = None,
               ended_by: str | None = None) -> list[str]:
        """The reset sentence first (once per conversation and boot), then the shellbox's
        notes."""
        out = []
        boot_id = res.get("boot_id")
        if boot_id and self.boots.get(ctx.conversation_id) != boot_id:
            self.boots[ctx.conversation_id] = boot_id
            out.append(boot_note(res))
        return out + [self._control_note(str(n), name, ended_by) or word_note(n)
                      for n in res.get("notes") or []]

    def _control_note(self, code: str, name: str | None,
                      ended_by: str | None = None) -> str | None:
        """What the user did to this terminal from the app (stopped:<program>:<epoch>,
        paused:..., resumed:..., closed:<epoch>), with the time in the app's zone. A close
        the app made itself (`ended_by` of the terminal of that name that ended last:
        done, idle) is worded as that."""
        key, _, rest = code.partition(":")
        if key not in ("stopped", "paused", "resumed", "closed"):
            return None
        program, _, epoch = rest.rpartition(":") if key != "closed" else ("", "", rest)
        try:
            at = datetime.fromtimestamp(float(epoch), zone(self.cfg)).strftime("%H:%M")
        except (TypeError, ValueError, OverflowError, OSError):
            return None
        user = self.cfg.user
        if key == "closed" and ended_by == "done":
            return f"Terminal {name} closed at {at}: the conversation that opened it was done."
        if key == "closed" and ended_by == "idle":
            return f"Terminal {name} closed at {at}: nothing ran in it for a day."
        if key == "closed":
            return f"{user} closed terminal {name} at {at}."
        if key == "stopped":
            return f"{user} stopped {program} (Ctrl-C) at {at}." if program else \
                f"{user} pressed Ctrl-C in {name} at {at}."
        return f"{user} {key} {program} at {at}."

    async def _result(self, ctx: ToolContext, res: dict, tool_name: str, name: str,
                      text: str = "", keys: list[str] | None = None,
                      notes: list[str] | None = None) -> ToolResult:
        name = res.get("terminal") or name
        state = res.get("state") or ("error" if res.get("error") else "")
        if state == "error" and res.get("full"):
            return self._refused(str(res.get("error")), name)   # the cap: no room made
        if state == "error":
            said = str(res.get("error") or "no reason given").strip().rstrip(".")
            return ToolResult(content=f"The shellbox answered but could not drive the "
                                      f"terminal: {said}.",
                              is_error=True, display={"terminal": name, "state": "error"})
        if state == "busy":
            wedge = self._wedge(ctx) if res.get("reason") != "paused" else None
            return self._refused(" ".join(filter(None, [self._busy(res, name), wedge])), name)
        prog, pid = program_of(res)
        is_run = "cmd" in res and isinstance(res.get("cmd"), str)
        lines = [self._status(res, name, prog, pid, is_run)]
        ended_by = None
        if ctx.store is not None and any(str(n).startswith("closed:")
                                         for n in res.get("notes") or []):
            ended_by = await terminals.last_ended_by(ctx.store, name)
        lines += self._notes(ctx, res, name, ended_by) + (notes or [])
        lines += self._explain(ctx, res, name, state, prog, pid, is_run)
        if (tool_name == "run" and state == "waiting") or \
                (tool_name == "stop" and state not in ("done", "prompt", "closed", "ended")):
            lines.append(self._wedge(ctx))
        if is_run:
            self.told.add((ctx.conversation_id, res["cmd"]))
        else:
            lines += self._run_ended(ctx, res)
        output = res.get("output") if is_run else res.get("screen")
        if is_run and state == "done" and res.get("exit") == 0 and APT_INSTALL.search(text) \
                and not APT_ERROR.search(output or "") \
                and ctx.conversation_id not in self.apt_noted:
            self.apt_noted.add(ctx.conversation_id)
            lines.append(APT_LINE)
        if "No space left on device" in (output or "") + (res.get("tail") or "") \
                and ctx.conversation_id not in self.full_noted:
            self.full_noted.add(ctx.conversation_id)
            lines.append(FULL_LINE)
        head = len("\n".join(lines).split("\n"))     # its lines above the output
        if output:
            lines.append(output)
        if res.get("cut"):
            lines += [cut_marker(res, name), res.get("tail") or ""]
        if tool_name == "read" and is_run:
            nxt = res.get("next")
            lines.append(f"more: {call('shell__read', name, **{'from': nxt})}" if nxt
                         is not None else "[the end of the log so far]")

        owner = res.get("owner") or "celine"
        display = {"terminal": name, "terminal_id": res.get("id"), "owner": owner,
                   "state": state, "program": prog, "pid": pid,
                   "exit": res.get("exit") if is_run else None,
                   "seconds": res.get("seconds"), "running_for": res.get("running_for"),
                   "quiet_for": res.get("quiet_for"), "paused": bool(res.get("paused")),
                   "opened": bool(res.get("opened")), "cmd": res.get("cmd") if is_run else None,
                   "log": res.get("log"), "head": head, "text": first_line(text),
                   "keys": " ".join(keys or [])}
        if tool_name == "read":
            display["read"] = True
        if res.get("outside"):             # shellboxd: it reached the net or read a file from there
            display["outside"] = {"source": sources.shell_source(res.get("outside_from")),
                                  "by": None}
        if res.get("net"):                 # shellboxd: it reached the net (the guard's sink)
            display["net"] = res["net"]
        if ctx.store is not None and res.get("id"):
            mine = res.get("opened") and owner == "celine" and ctx.conversation_id
            await terminals.upsert(ctx.store, str(res["id"]), name, owner, now(),
                                   ctx.conversation_id if mine else None)
        return ToolResult(content="\n".join(lines).rstrip(), display=display)

    def _capped(self, ctx: ToolContext) -> bool:
        return self.wedges.get(ctx.conversation_id, 0) >= WEDGE_MAX

    def _cap_words(self) -> str:
        return (f"Refused: you hit {WEDGE_MAX} wedged terminals in this conversation, so "
                f"shell__run and new terminals are off here. Stop using the shell in this "
                f"conversation and report what happened: bugreport__file, or tell "
                f"{self.cfg.user}.")

    def _wedge(self, ctx: ToolContext) -> str:
        """One more wedge for this conversation (a run that waits for input or was refused,
        a stop that did not free the terminal), in words."""
        n = self.wedges[ctx.conversation_id] = self.wedges.get(ctx.conversation_id, 0) + 1
        if n < WEDGE_MAX:
            return (f"Wedge {n} of {WEDGE_MAX} in this conversation: at {WEDGE_MAX}, shell__run "
                    f"and new terminals are refused here.")
        return (f"Wedge {n} of {WEDGE_MAX} in this conversation: shell__run and new terminals "
                f"are now refused here. Stop using the shell in this conversation and report "
                f"what happened: bugreport__file, or tell {self.cfg.user}.")

    def _busy(self, res: dict, name: str) -> str:
        """Why a run was not sent, and how to get past it: one sentence."""
        reason, prog = res.get("reason"), program_of(res)[0]
        if reason == "paused":
            return (f"Not run: {self.cfg.user} paused {prog or 'a program'} in {name}; run "
                    f"it in another terminal.")
        if reason == "more_input":
            return (f"Not run: the shell in {name} waits for the rest of a line (its prompt is "
                    f"\"> \"); {call('shell__stop', name)} drops it.")
        pid = program_of(res)[1]
        what = f"{prog} (pid {pid})" if prog else None
        if res.get("waiting"):
            run = (f"command {res['cmd']} ({first_line(res.get('command') or '')})"
                   if res.get("cmd") else None)
            said = hint(prog, args_of(res, pid), "waiting", "")
            return (f"Not run: {what or run or 'a command'} waits for input in {name}"
                    f"{f', from {run}' if what and run else ''}, and the line would go to it. "
                    f"{said + ' ' if said else 'shell__type answers it, '}"
                    f"{call('shell__stop', name)} stops it, or run in another terminal.")
        if res.get("cmd"):
            return (f"Not run: {name} still runs command {res['cmd']} "
                    f"({first_line(res.get('command') or '')}) for "
                    f"{dur(res.get('running_for'))}; {call('shell__stop', name)} stops it, or "
                    f"run in another terminal.")
        return (f"Not run: {what or 'a command'} runs in {name} and the line would go to it; "
                f"shell__type talks to it, {call('shell__stop', name)} stops it, or run in "
                f"another terminal.")

    @staticmethod
    def _status(res: dict, name: str, prog: str | None, pid: int | None,
                is_run: bool) -> str:
        """Line 1: terminal, directory, what happened; a run's log at the end."""
        state = res.get("state")
        where = f"terminal {name}" + (" (new)" if res.get("opened") else "")
        cwd = shellbox.home_short(res.get("cwd") or "")
        where += f" {cwd}" if cwd else ""
        front = f"{prog} (pid {pid})" if prog else None
        took = dur(res.get("seconds"), exact=True)
        if state == "closed":
            exit_part = f" (exit {res['exit']})" if not is_run and res.get("exit") is not None \
                else ""
            what = f"closed, the shell exited{exit_part}"
        elif is_run and state == "done":
            what = f"exit {res.get('exit')} after {took}"
        elif is_run and state == "running":
            what = f"still running after {took}" + (f": {front}" if front else "")
        elif is_run and state == "waiting":
            what = f"waiting for input after {took}: " + \
                (f"{front} reads the terminal" if front else "the command reads the terminal")
        elif is_run and state == "not_started":
            what = "not run"
        elif is_run and state == "ended":
            what = f"ended after {took}, exit unknown: the shell is back at its prompt"
        elif res.get("paused"):
            what = f"paused: {front or 'a program'}"
        elif state == "fullscreen":
            what = f"{front or 'a program'} has the full screen"
            cursor = res.get("cursor") or []
            if len(cursor) == 2:
                what += f", cursor at line {cursor[0]}, column {cursor[1]}"
            if res.get("highlighted"):
                what += f", highlighted: line {res['highlighted']}"
        elif state == "waiting":
            what = f"{front} waits for input" if front else "the shell waits for input"
        elif state == "running":
            what = f"{front} is running" if front else "a command is running in the shell"
            if res.get("running_for") is not None:
                what += f" for {dur(res['running_for'])}"
        else:
            what = "at the prompt"
        line = f"{where}: {what}"
        if is_run and state != "not_started":
            line += f". Log: {res.get('log')}"
        return line

    def _explain(self, ctx: ToolContext, res: dict, name: str, state: str,
                 prog: str | None, pid: int | None, is_run: bool) -> list[str]:
        """The fixed line of a state; a program's own hint where there is one, once per
        conversation and program: later results carry the status line and the output."""
        out = []
        size = int(res.get("size") or 0)
        killed, survived = res.get("killed"), res.get("survived")
        if killed:
            out.append(f"Ctrl-C had not stopped {' and '.join(killed['names'])}, so this stop "
                       f"ended it with {killed['signal']}.")
        if survived:
            out.append(f"Ctrl-C did not stop {' and '.join(survived)}: it reads Ctrl-C as a "
                       f"key. {call('shell__stop', name)} again ends it (SIGTERM, then "
                       f"SIGKILL).")
        elif is_run and state in ("running", "waiting"):
            out.append(f"It keeps running: {call('shell__read', name, **{'from': size})} "
                       f"shows what it prints from here, {call('shell__stop', name)} stops it."
                       if state == "running" else
                       f"Answer with {call('shell__type', name, text='...')}, or "
                       f"{call('shell__stop', name)} stops it.")
        if is_run and state == "not_started":
            out.append(NO_RUN_LINE)
        if state == "closed":
            out.append(f"A new run or type in {name} opens a new terminal.")
        if state in ("waiting", "fullscreen") and not res.get("paused"):
            last = last_line(res.get("tail") or res.get("output") or res.get("screen") or "")
            found = hint(prog, args_of(res, pid), state, last)
            key = (ctx.conversation_id, prog or found or "")
            if found and key not in self.hinted:
                self.hinted.add(key)
                out.append(found)
            elif not found and state == "fullscreen" and not is_run:
                out.append(f"{prog or 'It'} reads single keys: use keys, or text with "
                           f"enter=false for a single letter.")
        return out

    def _run_ended(self, ctx: ToolContext, res: dict) -> list[str]:
        """On a screen: its latest run in that terminal ended since it was last told."""
        cmd = res.get("cmd")
        if not isinstance(cmd, dict) or cmd.get("exit") is None:
            return []
        key = (ctx.conversation_id, str(cmd.get("id")))
        if key in self.told:
            return []
        self.told.add(key)
        return [f"Command {cmd.get('id')} ({first_line(cmd.get('command') or '')}) ended: exit "
                f"{cmd['exit']} after {dur(cmd.get('seconds'), exact=True)}. Log: "
                f"{cmd.get('log')}"]

    async def _listing(self, ctx: ToolContext, data: dict) -> ToolResult:
        rows = [r for r in data.get("terminals") or [] if isinstance(r, dict)]
        lines = self._notes(ctx, data)
        if not rows:
            lines.insert(0, "No terminals open. shell__run opens one.")
        for r in rows:
            name = r.get("name")
            if r.get("dead"):
                exit_part = f", exit {r.get('exit')}" if r.get("exit") is not None else ""
                lines.append(f"{name}: ended{exit_part}")
                continue
            whose = ""
            if r.get("owner") and r["owner"] != "celine":
                whose = f", {self.cfg.user}'s terminal"
                row = await terminals.get(ctx.store, str(r.get("id"))) \
                    if ctx.store is not None and r.get("id") else None
                if row and row["may_type"]:
                    whose += ", you may type"
            elif ctx.store is not None and r.get("id"):
                whose = opened_words(await terminals.opener(ctx.store, str(r["id"])),
                                     ctx.conversation_id)
            prog, _ = program_of(r)
            if prog and r.get("paused"):
                doing = f"{prog} paused"
            elif prog and r.get("waiting"):
                doing = f"{prog} waits for input"
            elif prog or r.get("busy"):
                doing = f"{prog or 'a command'} running for {dur(r.get('running_for'))}"
            else:
                doing = "at the prompt"
            cmd = r.get("cmd") or {}
            last = (f", last run {cmd['id']} exit {cmd['exit']}" if cmd.get("exit") is not None
                    else f", run {cmd['id']} not ended") if cmd.get("id") else ""
            shell = r.get("shell") or "shell"
            cwd = shellbox.home_short(r.get("cwd") or "")
            lines.append(f"{name}{whose}: {shell} {cwd}, {doing}{last}".replace(" ,", ","))
        return ToolResult(content="\n".join(lines),
                          display={"terminals": [r.get("name") for r in rows]})


async def probe(cfg, services) -> dict[str, str]:
    """Settings, Features: the shellbox at the saved address answers /health and takes
    the app's key (/status). No saved address: the default one is tried, and an answer
    comes back with `url`, the address the setup form prefills."""
    saved = services_mod.url(cfg, "shellbox")
    url = saved or services_mod.default("shellbox")
    host = urlsplit(url).netloc
    try:
        async with shellbox.client(cfg, PROBE_WAIT) as c:
            (await c.get(url + "/health", timeout=PROBE_WAIT)).raise_for_status()
            r = await c.get(url + "/status", timeout=PROBE_WAIT)
    except httpx.HTTPError as e:
        if not saved:
            return {"state": "unset", "detail": not_set_up("the shellbox")}
        return {"state": "missing", "detail": unreachable_words("the shellbox", url, e)}
    found = {} if saved else {"url": url}
    if r.status_code in (401, 403):
        return {"state": "missing", "detail": bad_key("the shellbox", url), **found}
    if saved:
        return {"state": "ready", "detail": f"The shellbox at {host} answers."}
    return {"state": "ready", "detail": f"Found at {url}.", "url": url}
