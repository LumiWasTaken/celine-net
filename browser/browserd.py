"""browserd: Celine's browser, one Brave on a screen of its own. Standard library plus
python3-websockets, for the one CDP socket, python3-pil, for marks and click points
on a shot, and snapshot.py next to it, which turns the page's trees into its outline.

It runs as celine in the browser box (browser/Dockerfile), kept alive by run.sh. Xvnc is
the screen (DISPLAY, the window's size); Brave draws on it with its own profile ~/.browser, a
real browser profile, and answers CDP on its own debugging port, 127.0.0.1:CDP_PORT.
The outline and input never run a script in a page: the outline comes from the
accessibility tree and a DOM snapshot with a hit test, the page text from the same trees,
the HTML from DOM.getOuterHTML, input from Input events, marks are drawn on the PNG. Only
the vault's site check reads location, in one evaluate (SITE_JS). It
returns facts; the app (server/celine/skills/browser.py) words them.

    POST /do {"tab", "profile"?, "steps": [{"do", "url"?, "target"?, "text"?}, ...]}
        Runs the steps in order on that tab (opened blank when new; profile main or
        throwaway for a new tab) and answers with the page after the last one:
        {"tab", "profile", "url", "title", "loading", "status" (the HTTP status of the
        page), "error" (why it did not load), "dialog", "view": {"y", "h", "page"}
        (px: the view's top, its height, the page's height), "lines": [the outline,
        snapshot.py], "scrolls": [[id, px below, px above]], "done": steps run,
        "effects": [one per step run, run_step()], "notes"}. A step: open (url),
        click, hover (target: an id, e14, or a point of the view, x,y in px), type (text;
        target?: the field, its text replaced, or kept with mode append; a newline in the
        text presses Enter; secret: true, a vault entry the app filled in, the only text
        a password field takes, leaves no draft and no shot), select (target: a select, text: the option), wait (text:
        until those words are on the page, WAIT_MAX s), press (text: keys and chords
        separated by spaces: Enter, Control+a, Shift+Tab, x), scroll (text: down, up (a
        view's height), top, bottom or px, signed; target?: scrolls there or inside it),
        find (text: scrolls the next match into view), back, forward, reload, upload
        (target: a file field, text: paths in its home separated by spaces), accept or
        dismiss (a dialog; text answers a prompt), close (the tab; last), restart (the
        browser; last). "show": all (the default), main (the outline without navigation,
        banners, footers and sidebars) or none (no outline). A page whose outline is the
        one it got last on that tab comes as {"unchanged": true} without lines.
        "drafts": [{"url", "field", "text"}], text it typed on a page the tab has left
        since (a navigation, a crash, a restart), once.
        An effect: {"effect": navigated (url), tab (url: a page it opened in a new tab,
        followed), dialog, download (file), changed (added, gone: counts; lines: the
        first new lines of the whole page, without ids), focus (to: the focused line),
        none, scrolled, found, not_found, waited or not_waited, "shot": the view after
        the step, a JPEG in base64, the click point and the target's box drawn on it;
        none under a dialog}.
        Before a click, hover or type into a target: the target is scrolled into view
        and must hold still, be enabled and be what the hit test finds at its centre.
    POST /do {"tab", "do": "look", "what": view, page, html, source or shot, "target"?:
        an id (view: that element's subtree, whole), "find"?: words or /a regex/ (view:
        the matching lines of the whole page with a line around each), "wait"?: words to
        wait for first, "shot"?: plain or marks, "screen"?: n}
        -> view: the page as above, nothing done; page: {"markdown"}; html: {"html"};
           source: {"source": the document as the server sent it} or {"binary": bytes};
           shot: {"shot": a WxH PNG of the view in base64} with the page (marks: every
           id in view drawn on its element; screen: scrolled to screen n first, n - 1 view
           heights down)
    POST /control {"action": take, give or restart}, the user's, from the live view
        -> {"taken": since when or null}; restart as above
    POST /control {"action": front or close, "tab"}, the user's, from the tab strip:
        front brings that tab to the front (409 busy (tab) while another tab is in a
        call), close closes it as its close step does (409 busy while it is in a call)
        -> {"front": the tab in front or null}
    GET /status -> {"state": starting, up or down, "reason", "started", "taken", "front"
        (the tab in front, the one on the screen, or null), "screen": {"w", "h"} (the
        live view's size in px), "tabs": [{"name", "url", "title", "busy"}]}
    GET /health -> {"ok": true}, without the key: run.sh and the compose health check
    Authorization: Bearer <the app's key> on every other call. The box holds only its
    sha256 (BROWSER_KEY_HASH_FILE, default /etc/browser/key.hash).

A refusal is {"error": code, "done": steps run, "effects": theirs, ...facts}: 400 bad
(what; screens with what screen), no_back, no_forward, no_dialog, no_option (options),
no_select (role), no_file (file), password_field (target, none at the cursor: text that
is not secret, into a password field; nothing of it was typed), wrong_site (site: the
page's origin, or none: a vault entry into a page that is not one of the step's sites,
a field in a frame of another site, or a page still loading; nothing of it was typed);
401 key; 404 no_tab (tabs); 409 busy, tabs_full
(tabs), dialog (dialog: a step, or a look other than the view), id_gone (the page now),
disabled, moving (the target did not hold still), covered (by: {"role", "name", "id"?}
on top of the target), no_source, profile (the tab's own), taken (since: the user has
the browser); 503 starting or down (reason), gone (the browser went away during the
call); 504 no_answer (url, title: a command got no answer in COMMAND_WAIT seconds).

Ids: e<n> names an element of the page, the same number for the same element as long
as the page lives (snapshot.Ids). A navigation starts the numbering again.

Tabs have names like its terminals, at most MAX_TABS (a new one past that closes the
least recently used tab with nothing typed on its page), one call at a time each. Every
call brings its tab to the front first. A tab uses its profile (main) or, opened
with profile throwaway, a browser context of its own: none of its cookies, nothing on
disk, gone when the tab closes or the browser restarts. A link that opens a new tab
opens in the same tab. Frames of other sites (an embedded sign-in, a consent wall)
are attached as they appear and read like the rest of the page.

Notes, for its next result on any tab (crashed: on that tab): restarted:<epoch> (the
start routine reopened its tabs after browserd or Brave died), crashed:<tab>:<status>
(the page's process died; this call loaded it again), throwaway:<tab> (a throwaway
tab the start routine did not reopen), extensions_off (Brave started only without
its extensions), download:<file>, download_failed:<file> (in ~/Downloads),
taken:<from>:<to> (the user had the browser, epochs), waited:<text> and
not_waited:<text> (a look's wait), closed:<tab>:<url> (a tab closed to make room, the least recently used one with no
typed text on its page), beforeunload:stayed or beforeunload:left (the page asked
before leaving: it stayed when it had typed on it, left otherwise).

Recovery: one start routine for every start (start()). browserd never repairs a running
browser: when Brave or Xvnc dies or stops answering it exits, and run.sh starts it
again. The tab list (TABS_FILE: name and profile of each tab, the URL of a main one, and
"restart" when it stopped on purpose) is written on every navigation; the next start
reopens its main tabs.

Extensions: every folder in ~/.browser-extensions with a manifest.json, loaded at each
start. A broken one hangs Brave's start, so the second launch goes without them.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import itertools
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

import snapshot
from snapshot import Frame, Ids, Node, View

HOME = Path.home()
PROFILE = HOME / ".browser"
TABS_FILE = PROFILE / "celine-tabs.json"
DRAFTS_FILE = PROFILE / "celine-drafts.json"   # what it typed, per tab, until returned
DOWNLOADS = HOME / "Downloads"
# the app's key never enters the box, only its sha256, in a file compose mounts read-only
KEY_HASH_FILE = Path(os.environ.get("BROWSER_KEY_HASH_FILE", "/etc/browser/key.hash"))
KEY_HASH = KEY_HASH_FILE.read_text().strip() if KEY_HASH_FILE.is_file() else ""
PORT = int(os.environ.get("BROWSER_PORT", "8771"))
CDP_PORT = 9300                     # fixed, on 127.0.0.1 only
VNC_PORT = 5900                     # Xvnc, for the app's live view
DISPLAY = ":1"
LANGUAGES = "en-US,en"               # its languages, set in its profile at every start
FLAGS = [f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check",
         "--password-store=basic", "--window-position=0,0",
         "--window-size=1440,981",  # a 1440x900 page under 81 px of tabs and address bar
         "--disk-cache-dir=/tmp/cache", f"--remote-debugging-port={CDP_PORT}",
         "--remote-debugging-address=127.0.0.1", "--hide-crash-restore-bubble",
         "--ignore-gpu-blocklist"]  # WebGL on Mesa's llvmpipe
EXTENSIONS = HOME / ".browser-extensions"   # its unpacked extensions, one folder each

MAX_TABS = 4
LAUNCH_WAIT = 10                    # seconds for Brave's first CDP answer, twice
COMMAND_WAIT = 10                   # seconds for one CDP command
LOAD_WAIT = 10                      # seconds a navigation is waited for; then what is there.
                                    # With COMMAND_WAIT, a hung page still answers in 20 s
EFFECT_WAIT = 2                     # seconds a step waits at most for its effect
POLL = 0.25                         # seconds between two looks for that effect
SCROLLED = 0.5                      # seconds a scroll takes to land
STABLE_WAIT = 1                     # seconds a target may take to stop moving before a click
STILL = 3                           # seconds the outline waits at most for the page to stop changing
PING_EVERY = 5                      # seconds between two pings of Brave
PING_WAIT = 10                      # a ping unanswered this long: Brave hangs
KEYS_ONE_BY_ONE = 200               # longer text is inserted at once (Input.insertText)
STEPS_MAX = 20                      # steps in one call
WAIT_MAX = 10                       # seconds a wait step waits for its words
W, H = 1440, 900                    # the view (--window-size, Xvnc's screen), and every shot
# the live view's screen: the window and one px (Brave shrinks a window as large as the
# screen by a px, and the view with it), the view under 81 px of tabs and address bar
SCREEN_W, SCREEN_H = W + 1, H + 82
FONT = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
PINK = (255, 0, 102)

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")
POINT = re.compile(r"(\d+),(\d+)")  # a point of the view, x,y in px
ID = re.compile(r"e?(\d+)")
STEPS = {"open", "click", "hover", "type", "select", "press", "scroll", "find", "wait",
         "back", "forward", "reload", "upload", "accept", "dismiss", "close", "restart"}
KEYS = {"Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8, "Delete": 46, "Space": 32,
        "ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39, "PageUp": 33,
        "PageDown": 34, "Home": 36, "End": 35, "F1": 112, "F2": 113, "F3": 114, "F4": 115,
        "F5": 116, "F6": 117, "F7": 118, "F8": 119, "F9": 120, "F10": 121, "F11": 122,
        "F12": 123, "Insert": 45}
MODIFIERS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}
# editing shortcuts Brave does not run from a bare key event: the command goes along
COMMANDS = {"a": "selectAll", "c": "copy", "v": "paste", "x": "cut", "z": "undo", "y": "redo"}

STATUS: dict = {"state": "starting", "reason": None, "started": None,
                "taken": None,          # since when the user has the browser, epoch
                "front": None}          # the tab in front, the one on the screen
tabs: dict[str, "Tab"] = {}
notes: list[str] = []               # for its next result, on any tab
popups: dict[str, str] = {}         # a tab's target -> the page it just opened
downloads: dict[str, tuple[str, str]] = {}      # download guid -> (file name, its link)
began: list[str] = []               # every download that began, its file name
answers: dict[int, queue.Queue] = {}
ids = itertools.count(1)
TABS_LOCK = threading.RLock()       # opening and closing tabs; make_room closes under it
SAVE_LOCK = threading.Lock()        # writing TABS_FILE
ws = xvnc = chrome = None
spare: str | None = None            # a blank page, kept while no tab is open: Brave
stopping = False                    # quits with its last page; a restart is under way


class Problem(Exception):
    """A refusal: {"error": code, **facts} with this HTTP status."""

    def __init__(self, status: int, code: str, **facts):
        super().__init__(code)
        self.status, self.code, self.facts = status, code, facts


class NoAnswer(Exception):
    """A CDP command got no answer in time."""


class Crashed(NoAnswer):
    """The tab's page died under a command."""


class Gone(Exception):
    """The CDP socket closed under a command: browserd is on its way out."""


class CdpError(Exception):
    """Brave refused a command."""


class Tab:
    def __init__(self, name: str, target: str, url: str = "", context: str | None = None):
        self.name, self.target, self.url = name, target, url
        self.context = context              # a throwaway tab's browser context
        self.session = attach(target)
        self.title = ""
        self.lock = threading.Lock()        # one call at a time
        self.ids = Ids()                    # e<n> per element, for the page's life
        self.nodes: dict[int, Node] = {}    # the ids of its last outline on this tab
        self.frames: dict[str, str] = {}    # frames of other processes: frame id -> session
        self.status: int | None = None      # the page's HTTP status
        self.error: str | None = None       # why the page did not load
        self.document: str | None = None    # the request id of the main frame's document
        self.used = time.monotonic()        # its last call on it
        self.drafts: list[dict] = []        # {"url", "field", "text"}: what it typed
        self.last: tuple | None = None      # (url, lines) of its last outline here
        self.crashed = None                 # the crash status until its next call
        self.dialog = None                  # {"type", "message"} while one is open
        self.loading = False
        self.stopped = threading.Event()    # the main frame stopped loading
        self.navs = 0                       # main frame navigations, in the document too
        self.mouse = (W / 2, H / 2)

    @property
    def profile(self) -> str:
        return "throwaway" if self.context else "main"


def log(text: str) -> None:
    print(f"browserd: {text}", flush=True)


# ---- CDP -----------------------------------------------------------------------------

def cdp(method: str, params: dict | None = None, session: str | None = None,
        wait: float = COMMAND_WAIT) -> dict:
    """One command and its answer. A command to a tab stops waiting when its page dies
    (Crashed), and input when it opened a dialog: Brave answers that only once the
    dialog is closed."""
    n = next(ids)
    box = answers[n] = queue.Queue()
    msg = {"id": n, "method": method, "params": params or {}}
    tab = by_session(session) if session else None
    if session:
        msg["sessionId"] = session
    end = time.monotonic() + wait
    try:
        ws.send(json.dumps(msg))
        while True:
            try:
                reply = box.get(timeout=0.1)
                break
            except queue.Empty:
                if tab and tab.crashed:
                    raise Crashed(method) from None
                if tab and tab.dialog and method.startswith("Input."):
                    return {}
                if time.monotonic() > end:
                    raise NoAnswer(method) from None
    except ConnectionClosed:
        raise Gone() from None
    finally:
        answers.pop(n, None)
    if reply is None:
        raise Gone()
    if "error" in reply:
        raise CdpError(reply["error"].get("message", ""))
    return reply.get("result", {})


def attach(target: str) -> str:
    session = cdp("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
    cdp("Page.enable", session=session)
    cdp("Network.enable", {"maxResourceBufferSize": 0, "maxTotalBufferSize": 0}, session)
    # frames of other sites run in other processes: their pages come as targets of their own
    cdp("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False,
                                 "flatten": True, "filter": [{"type": "iframe", "exclude": False},
                                                             {"exclude": True}]}, session)
    return session


def read_socket() -> None:
    why = "the CDP socket closed"
    try:
        for raw in ws:
            msg = json.loads(raw)
            if "id" in msg:
                box = answers.get(msg["id"])
                if box:
                    box.put(msg)
            else:
                on_event(msg["method"], msg.get("params", {}), msg.get("sessionId"))
    except ConnectionClosed:
        pass
    except Exception as e:
        why = f"reading CDP failed: {type(e).__name__}: {e}"
    die(why)


def by_target(target: str) -> Tab | None:
    return next((t for t in list(tabs.values()) if t.target == target), None)


def by_session(session: str | None) -> Tab | None:
    return next((t for t in list(tabs.values()) if t.session == session), None)


def on_event(method: str, p: dict, session: str | None) -> None:
    """Runs in the reader thread: state only, never a CDP call (its answer would wait
    for this very thread)."""
    if method.startswith("Target."):
        info = p.get("targetInfo") or {}
        tab = by_target(p.get("targetId") or info.get("targetId"))
        if method == "Target.targetInfoChanged" and tab:
            tab.title = info["title"]
            if info["url"] != tab.url:
                tab.url = info["url"]
                save()
        elif method == "Target.targetCreated" and info.get("type") == "page" and \
                by_target(info.get("openerId")):
            popups[info["openerId"]] = info["targetId"]
        elif method == "Target.targetCrashed" and tab:
            tab.crashed = p["status"]
        elif method == "Target.targetDestroyed" and tab:
            tabs.pop(tab.name, None)        # closed from outside, by the page or the user
            if STATUS["front"] == tab.name:
                STATUS["front"] = None
            save()
            if tab.context:                 # in a thread: its answer comes to this one
                threading.Thread(target=cdp, args=("Target.disposeBrowserContext",
                                                   {"browserContextId": tab.context}),
                                 daemon=True).start()
        elif method == "Target.attachedToTarget" and info.get("type") == "iframe" \
                and (tab := by_session(session)):
            tab.frames[info["targetId"]] = p["sessionId"]      # the target id is the frame id
        elif method == "Target.detachedFromTarget" and (tab := by_session(session)):
            tab.frames = {f: s for f, s in tab.frames.items() if s != p["sessionId"]}
    elif method == "Browser.downloadWillBegin":
        downloads[p["guid"]] = (p["suggestedFilename"], p.get("url") or "")
        began.append(p["suggestedFilename"])
    elif method == "Browser.downloadProgress" and p["state"] in ("completed", "canceled"):
        name, link = downloads.pop(p["guid"], ("", ""))
        if p["state"] == "completed" and name and link:
            # where it came from, for the guard (shellbox net-tag reads the same tag).
            # note: Brave names a duplicate "name (1).ext", the event only the first
            try:
                os.setxattr(DOWNLOADS / name, "user.celine.origin", link.encode()[:4000])
            except OSError:
                pass
        notes.append(f"download:{name}" if p["state"] == "completed"
                     else f"download_failed:{name}")
    elif tab := by_session(session):
        if method == "Page.javascriptDialogOpening":
            tab.dialog = {"type": p["type"], "message": p["message"]}
        elif method == "Page.javascriptDialogClosed":
            tab.dialog = None
        elif method == "Page.frameStartedLoading" and p["frameId"] == tab.target:
            tab.loading = True
        elif method == "Page.frameStoppedLoading" and p["frameId"] == tab.target:
            tab.loading = False
            tab.stopped.set()
        elif method == "Page.frameNavigated" and p["frame"]["id"] == tab.target:
            tab.ids.reset()                  # a new document: its elements, numbered anew
            tab.nodes = {}
            tab.navs += 1
        elif method == "Page.navigatedWithinDocument" and p["frameId"] == tab.target:
            tab.navs += 1
        elif method == "Network.requestWillBeSent" and p.get("type") == "Document" \
                and p.get("frameId") == tab.target:
            tab.document = p["requestId"]
        elif method == "Network.responseReceived" and p.get("type") == "Document" \
                and p.get("frameId") == tab.target:
            tab.status = p["response"]["status"]
        elif method == "Network.loadingFailed" and p.get("requestId") == tab.document:
            tab.error = p.get("errorText") or "failed"


def die(why: str) -> None:
    """Brave or Xvnc is gone or hangs: calls waiting on it get 503, browserd exits,
    run.sh starts it again."""
    if stopping:
        return
    log(f"exiting: {why}")
    STATUS.update(state="starting", reason=why)
    chrome.kill()                        # dead already, or hung: nothing left to save
    for box in list(answers.values()):
        box.put(None)
    for tab in list(tabs.values()):
        tab.stopped.set()                # a call waiting for a page load gives up too
    time.sleep(0.5)                      # their answers go out
    os._exit(1)


# ---- the start routine ---------------------------------------------------------------

def start() -> None:
    """Every start: leftovers killed, Xvnc, the profile lock removed, Brave (twice at
    most), its tabs reopened at their last URLs."""
    global ws, xvnc, chrome, spare
    began = time.monotonic()
    STATUS["started"] = int(time.time())
    subprocess.run(["pkill", "-TERM", "-o", "-x", "brave"])   # the browser: it saves
    for _ in range(20):
        if subprocess.run(["pgrep", "-x", "brave"], stdout=subprocess.DEVNULL).returncode:
            break
        time.sleep(0.1)
    subprocess.run(["pkill", "-KILL", "-x", "brave"])
    subprocess.run(["pkill", "-KILL", "-x", "Xvnc"])
    for _ in range(20):
        if subprocess.run(["pgrep", "-x", "Xvnc"], stdout=subprocess.DEVNULL).returncode:
            break
        time.sleep(0.1)
    n = DISPLAY[1:]
    for lock in (Path(f"/tmp/.X{n}-lock"), Path(f"/tmp/.X11-unix/X{n}")):
        lock.unlink(missing_ok=True)
    # the app's live view on the box network, no password (only the app is on it); the
    # viewer's keys, mouse and pasted text only while the user has taken the browser
    # (control()). The screen is SCREEN_W by SCREEN_H
    xvnc = subprocess.Popen(["Xvnc", DISPLAY, "-geometry", f"{SCREEN_W}x{SCREEN_H}", "-depth", "24",
                             "-SecurityTypes", "None", "-rfbport", str(VNC_PORT),
                             "-AcceptKeyEvents=0", "-AcceptPointerEvents=0", "-AcceptCutText=0",
                             "-AllowOverride=AcceptKeyEvents,AcceptPointerEvents,AcceptCutText"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if Path(f"/tmp/.X11-unix/X{n}").exists():
            break
        time.sleep(0.1)
    # a lock left by another hostname (a recreated box) would hang Brave on a dialog
    for lock in PROFILE.glob("Singleton*"):
        lock.unlink(missing_ok=True)
    # its languages and developer mode (its unpacked extensions are off without it),
    # whatever the profile had: Brave writes Preferences as it runs and on exit, so only
    # now, before it starts
    prefs = PROFILE / "Default" / "Preferences"
    try:
        kept = json.loads(prefs.read_text())
    except (OSError, ValueError):
        kept = {}
    kept.setdefault("intl", {})["accept_languages"] = LANGUAGES
    kept.setdefault("extensions", {}).setdefault("ui", {})["developer_mode"] = True
    # its tabs are reopened from TABS_FILE below: Brave's own restore (after a kill it
    # counts as a crash) would open the old ones next to them, twice each on the screen
    kept.setdefault("session", {})["restore_on_startup"] = 5
    kept.setdefault("profile", {}).update(exit_type="Normal", exited_cleanly=True)
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(json.dumps(kept))
    DOWNLOADS.mkdir(exist_ok=True)
    found = ",".join(str(m.parent) for m in sorted(EXTENSIONS.glob("*/manifest.json")))
    # twice at most, the second time without extensions: a broken one hangs the start
    version = None
    for load in ([f"--load-extension={found}"] if found else [], []):
        chrome = subprocess.Popen(["brave-browser", *FLAGS, *load, "about:blank"],
                                  env={**os.environ, "DISPLAY": DISPLAY},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        version = first_answer(chrome)
        if version:
            break
        reason = f"exited:{chrome.returncode}" if chrome.poll() is not None else "no_answer"
        chrome.kill()
        chrome.wait()
    if not version:
        STATUS.update(state="down", reason=reason)
        log(f"down: {reason}")
        return
    if found and not load:
        notes.append("extensions_off")
    ws = connect(version["webSocketDebuggerUrl"], max_size=None, ping_interval=None,
                 compression=None)
    threading.Thread(target=read_socket, daemon=True).start()
    cdp("Target.setDiscoverTargets", {"discover": True})
    cdp("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(DOWNLOADS),
                                        "eventsEnabled": True})
    spare = next(t["targetId"] for t in cdp("Target.getTargets")["targetInfos"]
                 if t["type"] == "page")
    try:
        saved = json.loads(TABS_FILE.read_text())
    except (OSError, ValueError):
        saved = {"tabs": []}
    try:
        drafts = json.loads(DRAFTS_FILE.read_text())
    except (OSError, ValueError):
        drafts = {}
    for t in saved["tabs"][:MAX_TABS]:
        if t["profile"] == "throwaway":     # gone with the browser it lived in
            notes.append(f"throwaway:{t['name']}")
            continue
        target = cdp("Target.createTarget", {"url": t["url"]})["targetId"]
        tabs[t["name"]] = Tab(t["name"], target, t["url"])
        # typed before the restart: the page lost it, its next result there gets it
        tabs[t["name"]].drafts = [{**d, "lost": True} for d in drafts.get(t["name"], [])]
    if tabs:
        cdp("Target.closeTarget", {"targetId": spare})
        spare = None
        to_front(list(tabs.values())[-1])
        if saved.get("stopped") != "restart":
            notes.append(f"restarted:{STATUS['started']}")
    STATUS.update(state="up", reason=None)
    save()
    log(f"up in {time.monotonic() - began:.1f} s, {len(tabs)} tabs reopened")


def first_answer(proc: subprocess.Popen) -> dict | None:
    end = time.monotonic() + LAUNCH_WAIT
    while time.monotonic() < end and proc.poll() is None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version",
                                        timeout=1) as r:
                return json.load(r)
        except OSError:
            time.sleep(0.1)
    return None


def save(stopped: str | None = None) -> None:
    """Only while up: a browser that is starting or down has not got all its tabs."""
    if STATUS["state"] != "up":
        return
    # a throwaway tab leaves no address on disk: its name, for the note at the next start
    data = {"tabs": [{"name": t.name, "profile": t.profile} if t.context else
                     {"name": t.name, "url": t.url, "profile": t.profile}
                     for t in list(tabs.values())],
            "stopped": stopped}
    with SAVE_LOCK:
        tmp = TABS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(TABS_FILE)


def watch() -> None:
    """Brave and Xvnc are alive and Brave answers, or browserd exits."""
    while True:
        time.sleep(PING_EVERY)
        if STATUS["state"] != "up":
            continue
        if xvnc.poll() is not None:
            die("Xvnc ended")
        if chrome.poll() is not None:
            die("Brave ended")
        try:
            cdp("Browser.getVersion", wait=PING_WAIT)
        except NoAnswer:
            die(f"Brave did not answer for {PING_WAIT} s")
        except Gone:
            pass                         # the reader is exiting already


def restart() -> None:
    """On purpose: the tab list says so, Brave closes cleanly, browserd exits."""
    global stopping
    stopping = True
    save(stopped="restart")
    was, STATUS["state"] = STATUS["state"], "starting"   # closing tabs now saves nothing
    if was == "up":
        try:
            cdp("Browser.close", wait=5)
        except (NoAnswer, Gone, CdpError):
            pass
        try:
            chrome.wait(5)
        except subprocess.TimeoutExpired:
            pass
    log("exiting: restart")
    os._exit(0)


# ---- one call ------------------------------------------------------------------------

def do_call(body: dict) -> dict:
    if STATUS["state"] != "up":
        raise Problem(503, STATUS["state"], reason=STATUS["reason"])
    name, profile = body.get("tab"), body.get("profile")
    look = body.get("do") == "look"
    steps = [] if look else body.get("steps")
    if not isinstance(name, str) or not NAME.match(name):
        raise Problem(400, "bad", what="tab")
    if not look and (not isinstance(steps, list) or not steps or len(steps) > STEPS_MAX
                     or any(not isinstance(s, dict) or s.get("do") not in STEPS for s in steps)):
        raise Problem(400, "bad", what="steps")
    if profile not in (None, "main", "throwaway"):
        raise Problem(400, "bad", what="profile")
    show = body.get("show", "all")
    if show not in ("all", "main", "none"):
        raise Problem(400, "bad", what="show")
    with TABS_LOCK:
        tab = tabs.get(name)
        if tab is None and (look or steps[0]["do"] != "open"):
            raise Problem(404, "no_tab", tabs=list(tabs))
        if tab is None:
            if len(tabs) >= MAX_TABS:
                make_room()
            new = {"url": "about:blank"}
            if profile == "throwaway":       # a window of its own, 1440x981 at 0,0 too
                new["browserContextId"] = cdp("Target.createBrowserContext")["browserContextId"]
            tab = Tab(name, cdp("Target.createTarget", new)["targetId"],
                      context=new.get("browserContextId"))
            tabs[name] = tab
            close_spare()
        elif profile and profile != tab.profile:
            raise Problem(409, "profile", profile=tab.profile)
    if not tab.lock.acquire(blocking=False):
        raise Problem(409, "busy")
    tab.used = time.monotonic()
    try:
        try:
            return run(tab, body) if look else run_steps(tab, steps, show)
        except Crashed:                  # its page died during the call: once more, which
            return run(tab, body) if look else run_steps(tab, steps, show)   # loads it again
    except NoAnswer:
        raise Problem(504, "no_answer", url=tab.url, title=tab.title) from None
    except Gone:
        raise Problem(503, "gone") from None
    finally:
        tab.lock.release()


def make_room() -> None:
    """The tabs are full: the least recently used idle tab with nothing typed on its
    page closes, with a note that says where it was; all of them busy or with typed
    text: tabs_full."""
    idle = [t for t in tabs.values() if not t.lock.locked()
            and not any(d["url"] == t.url for d in t.drafts)]
    if not idle:
        raise Problem(409, "tabs_full", tabs=list(tabs))
    old = min(idle, key=lambda t: t.used)
    close(old)
    notes.append(f"closed:{old.name}:{old.url}")


def close_spare() -> None:
    global spare
    if spare:
        cdp("Target.closeTarget", {"targetId": spare})
        spare = None


def crashed_note(tab: Tab, here: list[str]) -> None:
    """A page that died since its last call is loaded again first."""
    if tab.crashed:
        here.append(f"crashed:{tab.name}:{tab.crashed}")
        tab.crashed = None
        for d in tab.drafts:
            d["lost"] = True
        navigate(tab, tab.url)


def run_steps(tab: Tab, steps: list[dict], show: str = "all") -> dict:
    """The steps in order, each with its effect (effect()) and a shot of the view after
    it for the user. Text that is not a vault entry, into a password field of its last
    outline, is refused before anything runs."""
    for step in steps:
        if step["do"] == "type" and not step.get("secret") and step.get("target") is not None:
            m = ID.fullmatch(str(step["target"]).strip())
            node = tab.nodes.get(int(m[1])) if m else None
            if node is not None and node.password:
                raise Problem(400, "password_field", target=str(step["target"]), done=0,
                              effects=[])
    here: list[str] = []
    crashed_note(tab, here)
    effects: list[dict] = []
    aimed = None
    try:
        for step in steps:
            if step["do"] == "close":
                return close(tab)
            if step["do"] == "restart":
                return {"restarting": True}
            out, aimed = run_step(tab, step)
            effects.append(out)
    except Problem as e:
        e.facts["done"] = len(effects)
        e.facts["effects"] = effects
        raise
    page = result(tab, here, show)
    if effects and "shot" in effects[-1]:
        # the outline waited for the page to settle: the last shot shows the same page
        # (a login dialog drawn after the load event was blank in the shot before)
        effects[-1]["shot"] = step_shot(tab, aimed)
    return {**page, "done": len(effects), "effects": effects}


def run_step(tab: Tab, step: dict) -> tuple[dict, tuple | None]:
    """One step and what it did: {"effect", ...} (effect()), with "shot", the view after
    it as a JPEG in base64, the click point and the target's box drawn on it; and the
    point and box it aimed at, if any."""
    do, text = step["do"], step.get("text")
    to_front(tab)
    if tab.dialog and do not in ("accept", "dismiss"):
        raise Problem(409, "dialog", dialog=tab.dialog)
    if do in ("type", "press", "find", "wait", "select", "upload") and (not isinstance(text, str) or not text):
        raise Problem(400, "bad", what="text")
    aimed = None
    if do == "open":
        act(tab, step, {})
        out = {"effect": "navigated", "url": facts(tab)["url"]}
    elif do == "wait":
        out = {"effect": "waited" if wait_for(tab, step.get("text")) else "not_waited"}
    elif do == "find":
        out = {"effect": "found" if find(tab, step.get("text")) else "not_found"}
    elif do == "scroll":
        before = view_y(tab)
        act(tab, step, {})
        out = {"effect": "scrolled" if abs(view_y(tab) - before) >= 1 else "none"}
    else:
        before = fingerprint(tab)
        aimed = act(tab, step, before["parents"], before["focus"])
        out = effect(tab, before)
        unload(tab)
    # a page under a dialog may not answer; a vault entry typed leaves no picture
    if not tab.dialog and not step.get("secret"):
        out["shot"] = step_shot(tab, aimed)
    return out, aimed


# the focused line of an outline (fingerprint()) when it is a password field
PASSWORD = re.compile(r"^\s*- password field\b")

# the origin of the page and of the document that holds the cursor, through frames of
# the same site; a frame of another site holds it -> frame null
SITE_JS = """(() => {
  let d = document;
  for (let i = 0; i < 10; i++) {
    const a = d.activeElement;
    if (!a || !/^I?FRAME$/.test(a.tagName)) break;
    let c = null;
    try { c = a.contentDocument; } catch (e) {}
    if (!c) return {top: location.href, frame: null};
    d = c;
  }
  return {top: location.href, frame: d.location.href};
})()"""


def origin_of(url: str | None) -> str | None:
    """https://host:port, or None: not https, no host, a user name in it. The app reads
    an entry's sites the same way (celine/vault origin())."""
    try:
        u = urllib.parse.urlsplit(url or "")
        port = u.port or 443
    except ValueError:
        return None
    if u.scheme != "https" or not u.hostname or "@" in u.netloc:
        return None
    return f"https://{u.hostname.lower().rstrip('.')}:{port}"


def on_its_site(tab: Tab, sites: list[str]) -> None:
    """Right before a vault entry is typed: the page, and the frame that holds the
    cursor, are one of the entry's sites, and nothing is loading. Problem wrong_site
    when not, before a key goes out."""
    got = {}
    if not tab.loading:
        try:
            got = cdp("Runtime.evaluate", {"expression": SITE_JS, "returnByValue": True},
                      tab.session)["result"].get("value") or {}
        except Exception:
            got = {}
    top, frame = origin_of(got.get("top")), origin_of(got.get("frame"))
    if tab.loading or top is None or top not in sites or frame != top:
        raise Problem(400, "wrong_site", site=top or "none")


def act(tab: Tab, step: dict, parents: dict, focus: str | None = None) -> tuple | None:
    """The step's input, nothing waited for; the point and box it aimed at, if any.
    `focus`: the focused line before it (fingerprint()). Text goes into a password field
    only when it is a vault entry (secret), which is inserted at once and leaves no
    draft."""
    s = tab.session
    do, text, target = step["do"], step.get("text"), step.get("target")
    tab.stopped.clear()
    aimed = None
    if do == "open":
        url = step.get("url")
        if not isinstance(url, str) or not url:
            raise Problem(400, "bad", what="url")
        navigate(tab, url)
    elif do == "click":
        aimed = aim(tab, target, parents)
        press(tab, *aimed[:2])
    elif do == "hover":
        aimed = aim(tab, target, parents)
        move(tab, *aimed[:2])
    elif do == "type":
        mode = step.get("mode", "replace")
        if mode not in ("replace", "append"):
            raise Problem(400, "bad", what="mode")
        secret = bool(step.get("secret"))
        field, into = "the cursor", focus      # an id's field was checked (run_steps)
        if target is not None:
            m = ID.fullmatch(str(target).strip())
            node = tab.nodes.get(int(m[1])) if m else None
            field, into = (f"{node.role} {target}" if node else str(target)), None
            aimed = aim(tab, target, parents)
            press(tab, *aimed[:2])
            if spot(target) is not None and not secret:
                into = fingerprint(tab)["focus"]    # where the click put the cursor
        if into and PASSWORD.match(into) and not secret:
            raise Problem(400, "password_field",
                          **({} if target is None else {"target": str(target)}))
        if target is not None:
            if mode == "replace":
                key_event(tab, "a", modifiers=2, commands=["selectAll"])   # typing replaces
            else:
                key_event(tab, "End", modifiers=2)                         # to the end
        if secret:
            on_its_site(tab, step.get("sites") or [])
        if len(text) > KEYS_ONE_BY_ONE or secret:
            cdp("Input.insertText", {"text": text.rstrip("\n")}, s)
            if text.endswith("\n"):
                key_event(tab, "Enter")
        else:
            for ch in text:
                key_event(tab, "Enter" if ch == "\n" else ch)
                time.sleep(0.03)
        if not secret:
            draft(tab, field, text, mode)
    elif do == "select":
        choose(tab, target, text)
    elif do == "press":
        for chord in text.split():
            keys = chord.split("+")
            mods = sum(MODIFIERS[k] for k in keys[:-1] if k in MODIFIERS)
            if len(keys[:-1]) != sum(1 for k in keys[:-1] if k in MODIFIERS) or \
                    not (keys[-1] in KEYS or len(keys[-1]) == 1):
                raise Problem(400, "bad", what="key")
            key_event(tab, keys[-1], modifiers=mods,
                      commands=[COMMANDS[keys[-1].lower()]] if mods & 6 and keys[-1].lower()
                      in COMMANDS else None)
            time.sleep(0.05)
    elif do == "scroll":
        scroll(tab, target, text)
    elif do in ("back", "forward"):
        h = cdp("Page.getNavigationHistory", session=s)
        i = h["currentIndex"] + (1 if do == "forward" else -1)
        if not 0 <= i < len(h["entries"]) or h["entries"][i]["url"] == "about:blank":
            raise Problem(400, "no_" + do)        # a new tab starts blank
        cdp("Page.navigateToHistoryEntry", {"entryId": h["entries"][i]["id"]}, s)
    elif do == "reload":
        tab.status, tab.error = None, None
        cdp("Page.reload", session=s)
    elif do == "upload":
        node = node_of(tab, target)
        files = [str(HOME / f) if not f.startswith("/") else f for f in text.split()]
        for f in files:
            if not Path(f).is_file():
                raise Problem(400, "no_file", file=f)
        try:
            cdp("DOM.setFileInputFiles", {"files": files, "backendNodeId": node.backend},
                node.session)
        except CdpError:
            raise Problem(400, "bad", what="target") from None
    elif do in ("accept", "dismiss"):
        if not tab.dialog:
            raise Problem(400, "no_dialog")
        cdp("Page.handleJavaScriptDialog", {"accept": do == "accept",
                                            "promptText": text or ""}, s)
        tab.dialog = None
    return aimed


def unload(tab: Tab) -> None:
    """A page that asks before it is left: it stays when it has typed on it, is left
    otherwise; its result notes which."""
    if tab.dialog and tab.dialog["type"] == "beforeunload":
        typed = any(d["url"] == tab.url and not d.get("lost") for d in tab.drafts)
        cdp("Page.handleJavaScriptDialog", {"accept": not typed}, tab.session)
        tab.dialog = None
        notes.append("beforeunload:stayed" if typed else "beforeunload:left")
        if not typed:
            loaded(tab)


def fingerprint(tab: Tab) -> dict:
    """What a step may change: the navigations so far, the downloads begun, the whole
    page's outline without ids and without the new and active tags (bare()), the
    focused line, and every node's parent for the hit test before a click."""
    top, view = trees(tab, settle_first=False)
    got = snapshot.render(top, view, Ids(), lambda *a: True, whole=True)
    focus = next((bare(ln) for ln in got.lines if ACTIVE.search(ln)), None)
    parents = {f.session: parents_of(f.snap) for f in snapshot.frames_in(top)}
    return {"navs": tab.navs, "loading": tab.loading, "downloads": len(began),
            "lines": [bare(ln) for ln in got.lines],
            "focus": focus, "parents": parents}


ACTIVE = re.compile(r"\[(?:[^\]]*, )?active(?:, [^\]]*)?\]:?$")
TAGS = re.compile(r" \[([^\]]*)\](:?)$")


def bare(line: str) -> str:
    """An outline line without its ids and without the tags new and active, so two
    outlines of one page compare equal when only the numbering or the focus moved."""
    line = re.sub(r"\[e\d+\]", "", line)
    line = re.sub(r" e\d+(?=$|[ :\[])", "", line)
    m = TAGS.search(line)
    if m:
        kept = [t for t in m[1].split(", ") if t not in ("new", "active")]
        line = line[:m.start()] + (f" [{', '.join(kept)}]" if kept else "") + m[2]
    return line


def effect(tab: Tab, before: dict) -> dict:
    """What the step did, the first of: a dialog, a new tab (followed), a navigation, a
    download, a different whole-page outline (then waited until it holds still, STILL
    s at most), within EFFECT_WAIT s; else the focus moved, else nothing.
    {"effect": dialog | tab | navigated | download | changed | focus | none, ...}"""
    end = time.monotonic() + EFFECT_WAIT
    while True:
        time.sleep(POLL)
        if tab.dialog:
            return {"effect": "dialog"}
        if (new := popups.pop(tab.target, None)):
            follow(tab, new)
            loaded(tab)
            return {"effect": "tab", "url": facts(tab)["url"]}
        if tab.navs != before["navs"] or (tab.loading and not before["loading"]):
            loaded(tab)
            return {"effect": "navigated", "url": facts(tab)["url"]}
        if len(began) != before["downloads"]:
            return {"effect": "download", "file": began[-1]}
        now = fingerprint(tab)
        if tab.navs != before["navs"]:
            continue                         # it navigated while being read: say that
        if now["lines"] != before["lines"]:
            still = time.monotonic() + STILL
            while time.monotonic() < still:
                time.sleep(POLL)
                again = fingerprint(tab)
                if again["lines"] == now["lines"]:
                    break
                now = again
            return {"effect": "changed", **diff(before["lines"], now["lines"])}
        if time.monotonic() >= end:
            if now["focus"] and now["focus"] != before["focus"]:
                return {"effect": "focus", "to": now["focus"]}
            return {"effect": "none"}


SHOWN_NEW = 8                        # new lines a changed step shows


def diff(old: list[str], new: list[str]) -> dict:
    """The lines that came and how many went, as a multiset: a line twice on the page
    counts twice."""
    left = Counter(old)
    added = []
    for ln in new:
        if left[ln]:
            left[ln] -= 1
        else:
            added.append(ln)
    return {"added": len(added), "gone": sum(left.values()), "lines": added[:SHOWN_NEW]}


def follow(tab: Tab, new: str) -> None:
    """A page the tab opened in a new tab: the tab follows it, the old page closes."""
    old, tab.target, tab.session = tab.target, new, attach(new)
    tab.ids.reset()
    tab.nodes, tab.frames = {}, {}
    cdp("Target.closeTarget", {"targetId": old})
    to_front(tab)


def loaded(tab: Tab) -> None:
    """A navigation that started: until its page loaded, LOAD_WAIT s at most."""
    time.sleep(POLL)
    if tab.loading:
        tab.stopped.wait(LOAD_WAIT)


def view_y(tab: Tab) -> float:
    return cdp("Page.getLayoutMetrics", session=tab.session)["cssVisualViewport"]["pageY"]


def step_shot(tab: Tab, aimed: tuple | None) -> str:
    """The view as a JPEG in base64, for the user: where a click went as a pink ring,
    the target's box around it."""
    png = base64.b64decode(cdp("Page.captureScreenshot", {"format": "png"}, tab.session)["data"])
    im = Image.open(io.BytesIO(png)).convert("RGB")
    if aimed:
        x, y, box = aimed
        pen = ImageDraw.Draw(im)
        if box:
            pen.rectangle(box, outline=PINK, width=2)
        pen.ellipse((x - 12, y - 12, x + 12, y + 12), outline=PINK, width=3)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=75)
    return base64.b64encode(buf.getvalue()).decode()


def run(tab: Tab, body: dict) -> dict:
    """A look: nothing done. view: the page; page: its Markdown; html: its HTML with
    shadow roots; shot: a PNG of the view and the page."""
    here: list[str] = []
    crashed_note(tab, here)
    s = tab.session
    to_front(tab)
    what = body.get("what", "view")
    plain = what == "view" and not body.get("find") and body.get("target") is None \
        and not body.get("wait")
    if tab.dialog and not plain:            # a page under a dialog may not answer
        raise Problem(409, "dialog", dialog=tab.dialog)
    if isinstance(body.get("wait"), str) and body["wait"]:
        here.append(("waited:" if wait_for(tab, body["wait"]) else "not_waited:") + body["wait"])
    if what == "view" and body.get("find"):
        top, view = trees(tab)
        got = snapshot.render(top, view, tab.ids, lambda *a: True, whole=True)
        tab.nodes.update(got.nodes)
        lines, found = snapshot.find(got.lines, str(body["find"]))
        return {**facts(tab), "lines": lines, "found": found, "notes": take_notes() + here}
    if what == "view" and body.get("target") is not None:
        node = node_of(tab, body["target"])
        top, view = trees(tab)
        got = snapshot.render(top, view, tab.ids, lambda *a: True, scope=(node.session, node.backend))
        tab.nodes.update(got.nodes)
        return {**facts(tab), "lines": got.lines, "notes": take_notes() + here}
    if what == "view":                      # asked for: the outline, even if unchanged
        return result(tab, here, body.get("show", "all"), compare=False)
    if what == "page":
        top, view = trees(tab)
        return {**facts(tab), "markdown": snapshot.page(top, view), "notes": take_notes() + here}
    if what == "html":
        root = cdp("DOM.getDocument", {"depth": 0}, s)["root"]["nodeId"]
        html = cdp("DOM.getOuterHTML", {"nodeId": root, "includeShadowDOM": True}, s)["outerHTML"]
        return {**facts(tab), "html": html, "notes": take_notes() + here}
    if what == "source":
        return {**facts(tab), **source(tab), "notes": take_notes() + here}
    if what == "shot":
        if body.get("screen") is not None:
            to_screen(tab, body["screen"])
        return {**shot(tab, body.get("shot", "plain")), "notes": take_notes() + here}
    raise Problem(400, "bad", what="what")


def source(tab: Tab) -> dict:
    """The page's document as the server sent it: {"source": its text}, or {"binary":
    its size in bytes} when it is not text."""
    url = cdp("Target.getTargetInfo", {"targetId": tab.target})["targetInfo"]["url"]
    try:
        got = cdp("Page.getResourceContent", {"frameId": tab.target, "url": url}, tab.session)
    except CdpError:
        raise Problem(409, "no_source") from None
    if not got["base64Encoded"]:
        return {"source": got["content"]}
    raw = base64.b64decode(got["content"])
    try:
        return {"source": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return {"binary": len(raw)}


def to_screen(tab: Tab, n) -> None:
    """Screen n of the page in view: the view's top at n - 1 view heights."""
    m = cdp("Page.getLayoutMetrics", session=tab.session)
    h, page = m["cssVisualViewport"]["clientHeight"], m["cssContentSize"]["height"]
    screens = max(1, math.ceil(page / h - 0.01))
    if not isinstance(n, int) or not 1 <= n <= screens:
        raise Problem(400, "bad", what="screen", screens=screens)
    turn = (n - 1) * h - m["cssVisualViewport"]["pageY"]
    if abs(turn) >= 1:
        mouse(tab, "mouseWheel", W / 2, H / 2, deltaX=0, deltaY=turn)
        time.sleep(SCROLLED)


def draft(tab: Tab, field: str, text: str, mode: str) -> None:
    """A copy of what it typed, by page and field, on disk until it is returned."""
    text = text.rstrip("\n")
    if not text:
        return
    for d in tab.drafts:
        if d["url"] == tab.url and d["field"] == field and not d.get("lost"):
            d["text"] = d["text"] + text if mode == "append" else text
            break
    else:
        tab.drafts.append({"url": tab.url, "field": field, "text": text})
    save_drafts()


def save_drafts() -> None:
    data = {t.name: [d for d in t.drafts if not d.get("lost")] for t in list(tabs.values())}
    with SAVE_LOCK:
        tmp = DRAFTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: v for k, v in data.items() if v}))
        tmp.replace(DRAFTS_FILE)


def wait_for(tab: Tab, text: str) -> bool:
    """Until the words are somewhere on the page, WAIT_MAX seconds at most; whether
    they came."""
    want = " ".join(text.split()).lower()
    end = time.monotonic() + WAIT_MAX
    while True:
        for n in cdp("Accessibility.getFullAXTree", {}, tab.session)["nodes"]:
            if not n["ignored"] and want in snapshot.name_of(n).lower():
                return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.5)


def navigate(tab: Tab, url: str) -> None:
    """Waits for the page to load, LOAD_WAIT seconds at most; then it is what is there."""
    end = time.monotonic() + LOAD_WAIT
    tab.stopped.clear()
    tab.status, tab.error = None, None
    try:
        went = cdp("Page.navigate", {"url": url}, tab.session, wait=LOAD_WAIT)
    except NoAnswer:
        return
    if went.get("errorText"):
        tab.error = went["errorText"]
    if went.get("loaderId"):             # none: a jump inside the page
        tab.stopped.wait(max(0.0, end - time.monotonic()))


def close(tab: Tab) -> dict:
    """The tab closes. The notes wait for its next result: whoever closed it (make_room,
    a sub-agent that ended, the user) reads none. When it was in front, the tab it
    used last comes to the front."""
    global spare
    with TABS_LOCK:
        tabs.pop(tab.name, None)
        if not tabs:
            spare = cdp("Target.createTarget", {"url": "about:blank"})["targetId"]
        cdp("Target.closeTarget", {"targetId": tab.target})
        if tab.context:
            cdp("Target.disposeBrowserContext", {"browserContextId": tab.context})
        if STATUS["front"] == tab.name:
            STATUS["front"] = None
            if tabs:
                to_front(max(tabs.values(), key=lambda t: t.used))
    save()
    save_drafts()
    return {"closed": tab.name, "tabs": list(tabs), "notes": []}


def to_front(tab: Tab) -> None:
    cdp("Page.bringToFront", session=tab.session)
    STATUS["front"] = tab.name


def take_notes() -> list[str]:
    out = notes[:]
    del notes[:len(out)]
    return out


def facts(tab: Tab) -> dict:
    info = cdp("Target.getTargetInfo", {"targetId": tab.target})["targetInfo"]
    if info["url"] != tab.url:           # a page it followed into, see act()
        tab.url = info["url"]
        save()
    return {"tab": tab.name, "profile": tab.profile, "url": info["url"],
            "title": info["title"], "loading": tab.loading, "status": tab.status,
            "error": tab.error, "dialog": tab.dialog}


def result(tab: Tab, here: list[str], show: str = "all", compare: bool = True) -> dict:
    out = facts(tab)
    if not tab.dialog and show != "none":   # a page under a dialog may not answer
        out.update(outline(tab, chrome=show != "main"))
        same = compare and tab.last == (out["url"], out["lines"])
        tab.last = (out["url"], out["lines"])
        if same:                            # it has this already: say so instead
            del out["lines"]
            out["unchanged"] = True
    lost = [d for d in tab.drafts if d.get("lost") or d["url"] != out["url"]]
    if lost:                                # what it typed where the tab was: once
        out["drafts"] = [{k: d[k] for k in ("url", "field", "text")} for d in lost]
        tab.drafts = [d for d in tab.drafts if d not in lost]
        save_drafts()
    out["notes"] = take_notes() + here
    return out


# ---- the outline ---------------------------------------------------------------------

def trees(tab: Tab, settle_first: bool = True) -> tuple[Frame, View]:
    """The page's trees: the accessibility tree and one DOM snapshot of every document
    in the tab's process, the same of each frame of another process, stitched under
    their <iframe>, and the view."""
    s = tab.session
    # many pages build themselves after their load event: wait until one stops changing
    snap, end = None, time.monotonic() + (STILL if settle_first else 0)
    while True:
        before = snap
        snap = cdp("DOMSnapshot.captureSnapshot", {"computedStyles": ["overflow-y"],
                                                   "includeDOMRects": True}, s)
        if not settle_first or (before and count(before) == count(snap)) or time.monotonic() >= end:
            break
        time.sleep(0.3)
    metrics = cdp("Page.getLayoutMetrics", session=s)
    vv = metrics["cssVisualViewport"]
    view = View(vv["pageX"], vv["pageY"], vv["clientWidth"], vv["clientHeight"],
                metrics["cssContentSize"]["height"])
    strings, docs = snap["strings"], snap["documents"]
    # frames in the same process: their documents are in the snapshot, their trees by frame id
    frames = {}
    for doc in docs[1:]:
        fid = strings[doc["frameId"]]
        try:
            frames[fid] = cdp("Accessibility.getFullAXTree", {"frameId": fid}, s)["nodes"]
        except CdpError:
            pass
    top = snapshot.assemble(cdp("Accessibility.getFullAXTree", {}, s)["nodes"], snap, frames, s)
    # frames of other processes: their own trees, under the <iframe> whose frame id they are
    # note: one level; a frame of another process inside one is not attached
    for fid, fs in list(tab.frames.items()):
        for di, doc in enumerate(docs):
            nodes = doc["nodes"]
            for i, name in enumerate(nodes["nodeName"]):
                if strings[name] != "IFRAME" or nodes["backendNodeId"][i] in frames_known(top):
                    continue
                try:
                    owner = cdp("DOM.describeNode", {"backendNodeId": nodes["backendNodeId"][i]}, s)["node"]
                except CdpError:
                    continue
                if owner.get("frameId") != fid:
                    continue
                holder = top if di == 0 else frame_at(top, di)
                if holder is None:
                    break
                try:
                    ax = cdp("Accessibility.getFullAXTree", {}, fs)["nodes"]
                    fsnap = cdp("DOMSnapshot.captureSnapshot", {"computedStyles": ["overflow-y"],
                                                                "includeDOMRects": True}, fs)
                except CdpError:
                    break
                b = next((doc["layout"]["bounds"][k] for k, j in enumerate(doc["layout"]["nodeIndex"])
                          if j == i), [0, 0, 0, 0])
                holder.children[nodes["backendNodeId"][i]] = Frame(
                    ax, fsnap, 0, (holder.offset[0] + b[0], holder.offset[1] + b[1]), fs,
                    owner=nodes["backendNodeId"][i])
                break
    return top, view


def count(snap: dict) -> int:
    return sum(len(d["nodes"]["parentIndex"]) for d in snap["documents"])


def frame_at(top: Frame, doc: int) -> Frame | None:
    """The frame that holds snapshot document `doc`, anywhere under `top`."""
    for f in top.children.values():
        if f.snap is top.snap and f.doc == doc:
            return f
        if (deeper := frame_at(f, doc)) is not None:
            return deeper
    return None


def frames_known(top: Frame) -> set[int]:
    out = set(top.children)
    for f in top.children.values():
        out |= frames_known(f)
    return out


def parents_of(snap: dict) -> dict[int, int]:
    """Every node's parent across the snapshot's documents, a document's root under
    its <iframe>."""
    out = {}
    for doc in snap["documents"]:
        nodes = doc["nodes"]
        backend = nodes["backendNodeId"]
        for i, p in enumerate(nodes["parentIndex"]):
            if p >= 0:
                out[backend[i]] = backend[p]
        for i, child in zip(nodes["contentDocumentIndex"]["index"], nodes["contentDocumentIndex"]["value"]):
            cb = snap["documents"][child]["nodes"]["backendNodeId"]
            if cb:
                out[cb[0]] = backend[i]
    return out


def outline(tab: Tab, chrome: bool = True) -> dict:
    """The lines of what is in view and not covered, with the ids its next steps use."""
    top, view = trees(tab)
    parents = parents_of(top.snap)
    s = tab.session

    def reaches(frame: Frame, backend: int, x: float, y: float) -> bool:
        try:
            hit = cdp("DOM.getNodeForLocation", {"x": int(x), "y": int(y)}, s)["backendNodeId"]
        except CdpError:
            return False
        if frame.owner is not None:          # another process: the hit stops at its <iframe>
            return hit == frame.owner
        while hit is not None and hit != backend:
            hit = parents.get(hit)
        return hit == backend

    got = snapshot.render(top, view, tab.ids, reaches, chrome=chrome)
    tab.nodes.update(got.nodes)              # an id it saw earlier still works, scrolled away or not
    return {"view": {"y": round(view.y), "h": round(view.h), "page": round(view.page_h)},
            "lines": got.lines, "scrolls": [list(x) for x in got.scrolls]}


def node_of(tab: Tab, target) -> Node:
    m = ID.fullmatch(str(target).strip()) if target is not None else None
    node = tab.nodes.get(int(m[1])) if m else None
    if node is None:
        raise Problem(400, "bad", what="target", ids=sorted(tab.nodes))
    return node


# ---- input ---------------------------------------------------------------------------

def mouse(tab: Tab, kind: str, x: float, y: float, **extra) -> None:
    cdp("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, **extra}, tab.session)


def aim(tab: Tab, target, parents: dict) -> tuple:
    """Where a click on the target goes, (x, y, box) in view coordinates, after the
    checks a hand makes: a point of the view as it is; an element scrolled into view,
    refused when disabled, when it does not hold still for STABLE_WAIT s (moving), or
    when something else is on top of its centre (covered, by: that thing). `parents`:
    every node's parent by session (fingerprint())."""
    if (at := spot(target)) is not None:
        return (*at, None)
    node = node_of(tab, target)
    try:
        ax = cdp("Accessibility.getPartialAXTree", {"backendNodeId": node.backend,
                                                    "fetchRelatives": False}, node.session)["nodes"]
    except CdpError:
        raise Problem(409, "id_gone", **result(tab, [])) from None
    if ax and snapshot.prop(ax[0], "disabled"):
        raise Problem(409, "disabled")
    x, y, box, local = point_of(tab, node)
    hit = hit_at(tab.session, x, y)
    if node.frame.owner is not None:         # another process: the hit stops at its <iframe>
        if hit != node.frame.owner:
            raise Problem(409, "covered", by=cover(tab, tab.session, hit,
                                                    ancestors(parents.get(tab.session, {}),
                                                              node.frame.owner)))
        session, hit = node.session, hit_at(node.session, *local)
    else:
        session = tab.session
    up = parents.get(session, {})
    at = hit
    while at is not None and at != node.backend:
        at = up.get(at)
    if at is None:
        raise Problem(409, "covered", by=cover(tab, session, hit, ancestors(up, node.backend)))
    return x, y, box


def ancestors(up: dict, backend: int) -> set:
    out = {backend}
    while backend in up:
        backend = up[backend]
        out.add(backend)
    return out


def spot(target) -> tuple[int, int] | None:
    """A point of the view, x,y in px, or None for anything else (an id)."""
    m = POINT.fullmatch(str(target).strip()) if target is not None else None
    if m is None:
        return None
    x, y = int(m[1]), int(m[2])
    if not (x < W and y < H):
        raise Problem(400, "bad", what="target")
    return x, y


def point_of(tab: Tab, node: Node) -> tuple:
    """The node scrolled into view and holding still: its centre and box in view
    coordinates, and its centre in its own frame's."""
    try:
        cdp("DOM.scrollIntoViewIfNeeded", {"backendNodeId": node.backend}, node.session)
        quad = quad_of(node.backend, node.session)
        end = time.monotonic() + STABLE_WAIT
        while True:
            time.sleep(0.05)
            again = quad_of(node.backend, node.session)
            if again == quad:
                break
            if time.monotonic() >= end:
                raise Problem(409, "moving")
            quad = again
        dx = dy = 0.0
        if node.frame.owner is not None:     # the frame's own coordinates: add its <iframe>'s
            iq = quad_of(node.frame.owner, tab.session)
            dx, dy = min(iq[0::2]), min(iq[1::2])
    except (CdpError, IndexError):
        raise Problem(409, "id_gone", **result(tab, [])) from None
    lx, ly = sum(quad[0::2]) / 4, sum(quad[1::2]) / 4
    box = (min(quad[0::2]) + dx, min(quad[1::2]) + dy, max(quad[0::2]) + dx, max(quad[1::2]) + dy)
    return lx + dx, ly + dy, box, (lx, ly)


def quad_of(backend: int, session: str) -> list[float]:
    return cdp("DOM.getContentQuads", {"backendNodeId": backend}, session)["quads"][0]


def hit_at(session: str, x: float, y: float) -> int | None:
    """The node at view x, y of that session's document. The hit test takes page
    coordinates: the document's scroll is added."""
    try:
        v = cdp("Page.getLayoutMetrics", session=session)["cssVisualViewport"]
        return cdp("DOM.getNodeForLocation", {"x": int(x + v["pageX"]), "y": int(y + v["pageY"])},
                   session)["backendNodeId"]
    except (CdpError, KeyError):
        return None


def cover(tab: Tab, session: str, hit: int | None, under: set) -> dict:
    """What is on top, from the hit node up to where it meets the target's ancestors
    (`under`): the first that is structure, a control or named, else the first with any
    role, as {"role", "name", "id" (when it has one for it)}."""
    if hit is None:
        return {"role": "nothing", "name": ""}
    try:
        nodes = cdp("Accessibility.getPartialAXTree", {"backendNodeId": hit,
                                                       "fetchRelatives": True}, session)["nodes"]
    except CdpError:
        nodes = []
    by_id = {n["nodeId"]: n for n in nodes}
    n = next((n for n in nodes if n.get("backendDOMNodeId") == hit), None)
    chain = []
    while n is not None and n.get("backendDOMNodeId") not in under:
        if not n["ignored"] and n["role"]["value"] not in ("generic", "none", "StaticText",
                                                           "InlineTextBox"):
            chain.append(n)
        n = by_id.get(n.get("parentId"))
    known = snapshot.STRUCTURE | snapshot.INTERACTIVE
    pick = next((c for c in chain if c["role"]["value"] in known or snapshot.name_of(c)),
                chain[0] if chain else None)
    if pick is None:
        return {"role": "element", "name": ""}
    role, i = pick["role"]["value"], tab.ids.by_node.get((session, pick.get("backendDOMNodeId")))
    return {"role": snapshot.WORDS.get(role, role), "name": snapshot.name_of(pick),
            **({"id": f"e{i}"} if i else {})}


def move(tab: Tab, x: float, y: float) -> None:
    """To x, y in a few steps, as a hand moves."""
    x0, y0 = tab.mouse
    for step in range(1, 6):
        mouse(tab, "mouseMoved", x0 + (x - x0) * step / 5, y0 + (y - y0) * step / 5)
        time.sleep(0.02)
    tab.mouse = (x, y)


def press(tab: Tab, x: float, y: float) -> None:
    move(tab, x, y)
    mouse(tab, "mousePressed", x, y, button="left", clickCount=1)
    time.sleep(0.06)
    mouse(tab, "mouseReleased", x, y, button="left", clickCount=1)


def key_event(tab: Tab, key: str, modifiers: int = 0, commands: list | None = None) -> None:
    """One key down and up: a named key (KEYS) or one character, as typed, with the code
    and key code a page reads (games look at KeyW, not w)."""
    vk = KEYS.get(key) or (ord(key.upper()) if key.isascii() and key.isalnum() else 0)
    if key == "Space":
        key = " "
    code = key if key in KEYS else "Space" if key == " " else \
        f"Key{key.upper()}" if key.isascii() and key.isalpha() else \
        f"Digit{key}" if key.isascii() and key.isdigit() else ""
    down = {"type": "rawKeyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk,
            "modifiers": modifiers, "commands": commands or []}
    if key == "Enter":
        down.update(type="keyDown", text="\r")
    elif key not in KEYS and not modifiers:
        down.update(type="keyDown", text=key)
    cdp("Input.dispatchKeyEvent", down, tab.session)
    cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code,
                                   "windowsVirtualKeyCode": vk, "modifiers": modifiers},
        tab.session)


def choose(tab: Tab, target, text: str) -> None:
    """A <select>: focused, then the option typed, which is how a keyboard picks one;
    the page gets its change event. The option is matched by its text, whole or as a
    beginning, any case."""
    # note: type-ahead fires a change at each option it passes; a page that navigates
    # on change leaves early. The native popup is a widget CDP input does not reach.
    node = node_of(tab, target)
    if node.role != "select":
        raise Problem(400, "no_select", role=node.role)
    want = " ".join(text.split()).lower()
    option = next((o for o in node.options if o.lower() == want), None) or \
        next((o for o in node.options if o.lower().startswith(want)), None)
    if option is None:
        raise Problem(400, "no_option", options=node.options)
    try:
        cdp("DOM.focus", {"backendNodeId": node.backend}, node.session)
    except CdpError:
        raise Problem(409, "id_gone", **result(tab, [])) from None
    for ch in option:
        key_event(tab, ch)
        time.sleep(0.03)


def scroll(tab: Tab, target, text) -> None:
    """down or up a screen, top, bottom, or px (signed); with a target: the
    element into view, or, with a direction too, the wheel turned inside it (a scroll
    area)."""
    text = (text or "").strip().lower()
    if target is not None and not text:
        node = node_of(tab, target)
        try:
            cdp("DOM.scrollIntoViewIfNeeded", {"backendNodeId": node.backend}, node.session)
        except CdpError:
            raise Problem(409, "id_gone", **result(tab, [])) from None
        time.sleep(SCROLLED)
        return
    metrics = cdp("Page.getLayoutMetrics", session=tab.session)
    if text in ("top", "bottom") and target is None:
        page = metrics["cssContentSize"]["height"]
        turn = -page if text == "top" else page
    elif text in ("down", "up", ""):
        # a screen: the view's height, so screen n of the result is n downs from the top
        turn = metrics["cssVisualViewport"]["clientHeight"] * (-1 if text == "up" else 1)
        if target is not None:
            turn /= 2                              # scroll areas are smaller than the view
    elif re.fullmatch(r"-?\d+", text):
        turn = int(text)
    else:
        raise Problem(400, "bad", what="scroll")
    x, y = (W / 2, H / 2) if target is None else spot(target) or \
        point_of(tab, node_of(tab, target))[:2]
    move(tab, x, y)
    mouse(tab, "mouseWheel", x, y, deltaX=0, deltaY=turn)
    time.sleep(SCROLLED)


def find(tab: Tab, text: str) -> bool:
    """The next place the text is on the page, from the view's top on, scrolled into
    view; whether there was one."""
    want = " ".join(text.split()).lower()
    top, view = trees(tab, settle_first=False)
    hits: list[tuple[float, int, str]] = []

    def walk(frame: Frame):
        box, parent, *_ = snapshot.geometry(frame)
        for n in frame.ax:
            if n["ignored"] or n["role"]["value"] in snapshot.SKIP:
                continue
            name = snapshot.name_of(n)
            b = n.get("backendDOMNodeId")
            if b is None or want not in name.lower():
                continue
            at = box.get(b)
            while at is None and b in parent:
                b = parent[b]
                at = box.get(b)
            if at is not None:
                hits.append((at[1], n["backendDOMNodeId"], frame.session))
        for f in frame.children.values():
            walk(f)

    walk(top)
    hits.sort()
    nxt = next((h for h in hits if h[0] > view.y + 1), None) or (hits[0] if hits else None)
    if nxt is None:
        return False
    try:
        cdp("DOM.scrollIntoViewIfNeeded", {"backendNodeId": nxt[1]}, nxt[2])
    except CdpError:
        return False
    time.sleep(SCROLLED)
    return True


# ---- shots ---------------------------------------------------------------------------

def shot(tab: Tab, kind) -> dict:
    """A PNG of the view, in base64, with the page: plain, or marks (a pink box on every
    element with an id in view, the id in the box's top-right corner)."""
    if kind not in ("marks", "plain"):
        raise Problem(400, "bad", what="shot")
    out = {**facts(tab), **outline(tab)}
    png = base64.b64decode(cdp("Page.captureScreenshot", {"format": "png"}, tab.session)["data"])
    if kind == "marks":
        im = Image.open(io.BytesIO(png)).convert("RGB")
        pen = ImageDraw.Draw(im, "RGBA")
        font = ImageFont.truetype(FONT, 13)
        vy = out["view"]["y"]
        boxes = [(i, n.box) for i, n in sorted(tab.nodes.items()) if n.role != "scrollarea"]
        for _, (x, y, w, h) in boxes:
            pen.rectangle((x - 2, y - vy - 2, x + w + 1, y - vy + h + 1), outline=PINK, width=2)
        for i, (x, y, w, h) in boxes:
            label = f"e{i}"
            right, top = min(x + w, W), max(y - vy, 0)
            pen.rectangle((right - pen.textlength(label, font=font) - 6, top, right, top + 15),
                          fill=PINK)
            pen.text((right - 3, top + 8), label, font=font, fill="white", anchor="rm")
        buf = io.BytesIO()
        im.save(buf, "PNG")
        png = buf.getvalue()
    out["shot"] = base64.b64encode(png).decode()
    return out


# ---- take-over -----------------------------------------------------------------------

def control(action, name=None) -> dict:
    """The user takes the browser from the live view (Xvnc takes the viewer's keys, mouse
    and pasted text, its calls get 409 taken) or gives it back (its next result says when
    they had it). A restart of browserd gives it back without the note. front and close:
    tab_control."""
    if action in ("front", "close"):
        return tab_control(action, name)
    if action not in ("take", "give"):
        raise Problem(400, "bad", what="action")
    if action == "take" and STATUS["state"] != "up":
        raise Problem(503, STATUS["state"], reason=STATUS["reason"])
    now = int(time.time())
    if action == "take" and not STATUS["taken"]:
        STATUS["taken"] = now
    elif action == "give" and STATUS["taken"]:
        notes.append(f"taken:{STATUS['taken']}:{now}")
        STATUS["taken"] = None
    for name in ("AcceptKeyEvents", "AcceptPointerEvents", "AcceptCutText"):
        subprocess.run(["vncconfig", "-display", DISPLAY, "-set",
                        f"{name}={int(bool(STATUS['taken']))}"], timeout=5)
    return {"taken": STATUS["taken"]}


def tab_control(action: str, name) -> dict:
    """The user's tab strip. front: that tab to the front, refused while another tab is
    in a call (its page has to stay on the screen). close: as its close step, refused
    while the tab is in a call."""
    if STATUS["state"] != "up":
        raise Problem(503, STATUS["state"], reason=STATUS["reason"])
    tab = tabs.get(name) if isinstance(name, str) else None
    if tab is None:
        raise Problem(404, "no_tab", tabs=list(tabs))
    if action == "front":
        # note: a call that starts right after this check brings its own tab back
        # to the front, so the race only costs the user's click
        busy = next((t.name for t in list(tabs.values()) if t is not tab and t.lock.locked()), None)
        if busy:
            raise Problem(409, "busy", tab=busy)
        to_front(tab)
        return {"front": tab.name}
    if not tab.lock.acquire(blocking=False):
        raise Problem(409, "busy", tab=tab.name)
    try:
        close(tab)
    finally:
        tab.lock.release()
    return {"front": STATUS["front"]}


# ---- HTTP ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def authorized(self) -> bool:
        scheme, _, key = self.headers.get("Authorization", "").partition(" ")
        if scheme == "Bearer" and hmac.compare_digest(
                hashlib.sha256(key.encode()).hexdigest(), KEY_HASH):
            return True
        self.send_json({"error": "key"}, 401)
        return False

    def do_GET(self):
        if self.path == "/health":
            return self.send_json({"ok": True})
        if not self.authorized():
            return
        if self.path != "/status":
            return self.send_json({"error": "bad", "what": "path"}, 404)
        self.send_json({**STATUS, "screen": {"w": SCREEN_W, "h": SCREEN_H},
                        "tabs": [{"name": t.name, "url": t.url, "title": t.title,
                                  "busy": t.lock.locked()} for t in list(tabs.values())]})

    def do_POST(self):
        if not self.authorized():
            return
        if self.path not in ("/do", "/control"):
            return self.send_json({"error": "bad", "what": "path"}, 404)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))
                              or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.send_json({"error": "bad", "what": "body"}, 400)
        if self.path == "/control" and body.get("action") != "restart":
            try:
                return self.send_json(control(body.get("action"), body.get("tab")))
            except Problem as e:
                return self.send_json({"error": e.code, **e.facts}, e.status)
            except CdpError as e:
                return self.send_json({"error": "cdp", "message": str(e)}, 500)
        if self.path == "/do" and STATUS["taken"]:
            return self.send_json({"error": "taken", "since": STATUS["taken"]}, 409)
        steps = body.get("steps") if isinstance(body.get("steps"), list) else []
        if self.path == "/control" or (steps and steps == [{"do": "restart"}]):
            self.send_json({"restarting": True})
            self.wfile.flush()
            return restart()
        try:
            out = do_call(body)
        except Problem as e:
            return self.send_json({"error": e.code, **e.facts}, e.status)
        except CdpError as e:
            return self.send_json({"error": "cdp", "message": str(e)}, 500)
        self.send_json(out)
        if out.get("restarting"):           # a restart after other steps
            self.wfile.flush()
            restart()

    def send_json(self, data: dict, status: int = 200) -> None:
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


def main() -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", KEY_HASH):
        sys.exit(f"browserd: {KEY_HASH_FILE} is not the key's sha256 (64 lowercase hex)")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    start()
    watch()


if __name__ == "__main__":
    main()
