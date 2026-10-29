"""The agent's browser: one Brave in the browser box (browser/browserd.py), used like a person
at a screen.

Five tools. browser__open loads a page in a named tab and browser__close closes it
(each has effects: a GET can log out or start a download); browser__scroll scrolls it
(a reading tool, as look); browser__act runs steps on it
(click, type, select, hover, press, find, back, forward, reload, upload, accept,
dismiss, restart, and locate: a click or hover where the Screen reading model
boxes what it names); browser__look reads the page: the view again, the whole page
as Markdown, its HTML, its source, or the screen itself, as an image when its model sees images,
else as a model's answer to its question, or where something is (locate, vision.py).
The agent's own calls on one tab wait their turn (a lock per tab). open and act answer with the
page: the tab, address and title, a line per step on what it did, where the view is,
then the outline, one line per element with an id it acts on (browser/snapshot.py).

The box answers with facts (server/celine/browser/sync.py); every word it reads is
composed here, next to the guide (guides/browser.md), which quotes the numbers below:
change both together.
"""
from __future__ import annotations

import base64
import ipaddress
import logging
import math
import posixpath
import re
import socket
import unicodedata
from collections import defaultdict
from urllib.parse import unquote, urlsplit
from datetime import datetime
from typing import Annotated, Literal, NotRequired, Required, TypedDict

import anyio
import httpx

from .. import attachments, shares, vision
from ..browser import sync
from ..browser.sync import BrowserRefused
from ..turns import tokens
from .. import vault as vault_mod
from ..vault import EntryError, VaultError, reason
from . import ToolContext, ToolResult, guide, tool, described
from ..clock import zone
from ..errors import ToolRefused, bad_key, not_set_up, unreachable_words
from .web import split_parts

log = logging.getLogger("celine.browser")

MAX_TABS = 4           # tabs open at once (browserd's MAX_TABS)
STEPS_MAX = 20         # steps in one browser__act (browserd's STEPS_MAX)
PART_TOKENS = 6000     # one part of a page's text or HTML
OUTLINE_TOKENS = 4000  # the outline at most; more is cut and said
VIEW = (1440, 900)     # the view and every shot, px (browserd's W, H)
CDP_PORT = 9300        # its DevTools, on the box's loopback (browserd's CDP_PORT)

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")
POINT = re.compile(r"^(\d+)\s*,\s*(\d+)$")      # a point of the view, x,y in px
ID = re.compile(r"^e?(\d+)$", re.I)
NAME_RULE = ("A tab name is up to 16 lowercase letters, digits, - or _, starting with a "
             "letter or digit, like tab=\"jobs\".")
# scroll and close are tools of their own, browser__scroll and browser__close
DOES = ("click", "type", "select", "hover", "press", "find", "wait", "back",
        "forward", "reload", "upload", "accept", "dismiss", "restart", "locate")
NEEDS_TARGET = {"click", "hover", "select", "upload"}
NEEDS_TEXT = {"type", "select", "press", "find", "wait", "upload", "locate"}
THEN = ("click", "hover", "none")     # what a locate step does at the centre of its box
DID = {"click": "clicked", "hover": "hovered"}
SHOW = Literal["all", "main", "none"]
SHOW_HELP = ("What comes back: all, the outline of the view; main, the outline without "
             "navigation, banners, footers and sidebars; none, no outline, for a step you "
             "will look at later.")
# the key names browserd knows, written exactly so
KEYS = {"Enter", "Tab", "Escape", "Backspace", "Delete", "Space", "ArrowUp", "ArrowDown",
        "ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End", "Insert", "F1", "F2",
        "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12"}
MODS = {"Control", "Alt", "Shift", "Meta"}
KEY_NAMES = ("Enter, Tab, Escape, Backspace, Delete, Space, ArrowUp, ArrowDown, ArrowLeft, "
             "ArrowRight, PageUp, PageDown, Home, End, Insert, F1 to F12, or one single "
             "character; a chord joins them with + after Control, Alt, Shift or Meta, like "
             "Control+a or Shift+Enter")
CRASHES = {"killed": "killed, most likely out of memory", "oom": "out of memory"}
LAUNCH = {"no_answer": "Brave did not answer within 10 s, twice"}
SHOTS = {"id": "marks", "text": "plain"}   # what the answering model sees
TOP_WITH_TARGET = ("top and bottom are for the page; inside an element, scroll down or up, "
                   "or by a number of px.")
# a vault entry in a type step's text (vault/skill.py): filled in here, never by it. The
# name is any printable text up to the last /password or /previous, a / in it too
VAULT = re.compile(r"vault:([^\x00-\x1f\x7f]+)/(password|previous)(\n?)")
PASSWORD_FIELD = re.compile(r"^\s*- password field\b")    # an outline line (snapshot.py)


class Step(TypedDict):
    do: Required[Annotated[Literal["click", "type", "select", "hover", "press", "find",
                                   "wait", "back", "forward", "reload", "upload", "accept",
                                   "dismiss", "restart", "locate"], "What to do."]]
    target: NotRequired[Annotated[str, "An id from the page, like e14 (click, hover, type, "
                                       "select, upload), or a point of the screen in "
                                       "px from browser__look, like 712,340 (click, "
                                       "hover)."]]
    text: NotRequired[Annotated[str, "type: the text; a newline at its end presses Enter. "
                                     "select: the option. press: keys separated by spaces, "
                                     "like Enter, or Control+a, or ArrowDown ArrowDown Enter. "
                                     "find: words on the page. wait: words to wait for. "
                                     "upload: file paths in your home separated by spaces. "
                                     "accept: the answer to a prompt. locate: what to find on "
                                     "the screen, in words, like the blue Sign in button."]]
    mode: NotRequired[Annotated[Literal["replace", "append"],
                                "type: replace what the field holds (the default), or append "
                                "at its end."]]
    then: NotRequired[Annotated[Literal["click", "hover", "none"],
                                "locate: click the middle of what was found (the default), "
                                "hover there, or none, only say where it is."]]


def tab_name(tab: str | None) -> str:
    name = str(tab)
    if not NAME.match(name):
        raise ToolRefused(NAME_RULE)
    return name


# a host, maybe with a port and a path: example.com/jobs, localhost:8080
HOST = re.compile(r"^[\w-]+(\.[\w-]+)*(:\d+)?(/\S*)?$")
# addresses whose scheme has no // after it
NO_SLASHES = ("about:", "data:", "javascript:", "blob:", "view-source:", "mailto:")


def address(url: str) -> str:
    """example.com/jobs -> https://example.com/jobs; any other address as it is
    (chrome://settings, chrome-extension://..., file:///home/celine/..., about:blank)."""
    url = (url or "").strip()
    if "://" not in url and not url.lower().startswith(NO_SLASHES) and HOST.match(url):
        url = "https://" + url
    if not re.match(r"^[a-z][a-z0-9+.-]*:\S+$", url, re.I):
        raise ToolRefused("url is an address, like https://example.com/jobs or chrome://settings.")
    return url


# the cloud's instance metadata: the link-local range, and AWS's address on IPv6
METADATA = (ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fd00:ec2::254/128"))
DEFAULT_PORTS = {"http": 80, "ws": 80, "https": 443, "wss": 443, "ftp": 21}
SPECIAL = (*DEFAULT_PORTS, "file")     # schemes whose \ Brave reads as /
RESOLVE_WAIT = 3.0      # seconds to look a name up; a name that does not answer opens
IPV4_PART = re.compile(r"0[xX][0-9a-fA-F]*|0[0-7]*|[1-9][0-9]*")
DOTS = str.maketrans({"。": ".", "．": ".", "｡": "."})   # IDNA's full stops
IP = ipaddress.IPv4Address | ipaddress.IPv6Address


def ipv4(host: str) -> ipaddress.IPv4Address | None:
    """A host as Brave reads an IPv4 address: one to four parts, each decimal, 0x hex or
    0 octal (2130706433, 127.1 and 0x7f.0.0.1 are all 127.0.0.1); None for a name."""
    parts = host.split(".")
    if len(parts) > 1 and parts[-1] == "":
        parts.pop()
    if not 1 <= len(parts) <= 4 or not all(IPV4_PART.fullmatch(p) for p in parts):
        return None
    nums = [int(p[2:] or "0", 16) if p[:2].lower() == "0x" else int(p, 8 if p[0] == "0" else 10)
            for p in parts]
    if any(n > 255 for n in nums[:-1]) or nums[-1] >= 256 ** (5 - len(nums)):
        return None
    return ipaddress.IPv4Address(sum(n << 8 * (3 - i) for i, n in enumerate(nums[:-1]))
                                 + nums[-1])


def literal(host: str) -> IP | None:
    """The address a host names by itself, an IPv4 address in IPv6 read as IPv4."""
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0]) if ":" in host else ipv4(host)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def where(url: str) -> tuple[str, str, int | None, str]:
    """(scheme, host, port, path) of an address as Brave reads it: inside view-source:,
    \\ for / in a web address, the host percent-decoded, its full-width digits and dots
    plain, lower case, no dot at its end."""
    while url[:12].lower() == "view-source:":
        url = url[12:]
    scheme = url.split(":", 1)[0].lower()
    if scheme in SPECIAL:
        url = url.replace("\\", "/")
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return scheme, "", None, ""
    host = unicodedata.normalize("NFKC", unquote(p.hostname or "")).translate(DOTS)
    return scheme, host.lower().rstrip("."), port or DEFAULT_PORTS.get(scheme), p.path


async def addresses(host: str, port: int | None) -> list[IP]:
    """What a name points to, as the box's resolver would answer; [] when it does not
    answer in RESOLVE_WAIT."""
    with anyio.move_on_after(RESOLVE_WAIT):
        try:
            infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            return []
        return [ip for i in infos if (ip := literal(str(i[4][0]))) is not None]
    return []


def ours(ip: IP) -> bool:
    """Whether an address is one of the app's own, not its loopback: this machine takes
    it for a socket. The box's loopback is the box's."""
    if ip.is_loopback or ip.is_unspecified:
        return False
    with socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET) as s:
        try:
            s.bind((str(ip), 0))
        except OSError:
            return False
    return True


async def refusal(url: str) -> str | None:
    """Why the browser does not open an address, None when it does: its own DevTools
    on the box's loopback; the app where the box reaches it (shares.INSIDE), by that
    name or one of the app's own addresses, except its shares; the cloud's metadata.
    Each by its address or a name that points there. Every scheme, the LAN and the
    box's other ports open."""
    scheme, host, port, path = where(url)
    if not host:
        return None
    ip = literal(host)
    ips = [ip] if ip is not None else await addresses(host, port)
    loopback = host == "localhost" or host.endswith(".localhost") or \
        any(a.is_loopback or a.is_unspecified for a in ips)
    if loopback and port == CDP_PORT:
        return "That is your browser's own DevTools port, which no tab opens."
    if any(a in net for a in ips for net in METADATA if a.version == net.version):
        return "That is the cloud's instance metadata address, which no tab opens."
    app = urlsplit(shares.INSIDE)
    if port == app.port and (host == app.hostname or any(ours(a) for a in ips)) and \
            not posixpath.normpath("/" + unquote(path).lstrip("/")).startswith(shares.PREFIX):
        return (f"That is the app itself, which no tab opens; a share of it does, at "
                f"{shares.INSIDE}{shares.PREFIX}<token>/.")
    return None


def key_names(text: str) -> str:
    """The agent's keys and chords, checked against the names browserd knows."""
    out = []
    for chord in text.split():
        parts = chord.split("+") if chord != "+" else ["+"]
        names = []
        for i, p in enumerate(parts):
            last = i == len(parts) - 1
            if not ((not last and p in MODS) or p in KEYS or len(p) == 1):
                raise ToolRefused(f"Unknown key {p!r}. Keys: {KEY_NAMES}.")
            names.append(p)
        out.append("+".join(names))
    if not out:
        raise ToolRefused(f"press needs text: the keys, like \"Enter\". Keys: {KEY_NAMES}.")
    return " ".join(out)


def target_of(target) -> str:
    """An id (e14, or 14) or a point of the view (712,340, px)."""
    t = str(target).strip().rstrip(":,.")
    m = POINT.match(t)
    if m:
        x, y = int(m[1]), int(m[2])
        if x < VIEW[0] and y < VIEW[1]:
            return f"{x},{y}"
        raise ToolRefused(f"A point is x,y in px of the {VIEW[0]}x{VIEW[1]} screen.")
    m = ID.match(t)
    if m:
        return "e" + m[1]
    raise ToolRefused("target is an id from the page, like e14, or a point of the screen from "
                  "browser__look, like 712,340.")


def in_view(box: str) -> tuple[list[int], list[int]]:
    """A box "x1,y1,x2,y2" on 0 to 1000 of the shot (vision.box) -> its centre and the
    box in px of the view, each kept inside the view."""
    px = [min(VIEW[i % 2] - 1, round(int(v) * VIEW[i % 2] / 1000))
          for i, v in enumerate(box.split(","))]
    return [(px[0] + px[2]) // 2, (px[1] + px[3]) // 2], px


def into(target: str | None) -> str:
    """Where a type step's text goes, in words: into e14, into the field at 120,300, or
    at the cursor (target_of() wrote the target)."""
    if target is None:
        return "at the cursor"
    return f"into the field at {target}" if POINT.match(target) else f"into {target}"


def steps_of(steps) -> list[dict]:
    """The agent's steps checked and worded for browserd."""
    if not isinstance(steps, list) or not steps:
        raise ToolRefused("steps is a list of steps, like [{\"do\": \"click\", \"target\": \"e14\"}].")
    if len(steps) > STEPS_MAX:
        raise ToolRefused(f"At most {STEPS_MAX} steps in one call.")
    out = []
    for i, step in enumerate(steps, 1):
        if isinstance(step, dict) and step.get("do") in ("scroll", "close"):
            raise ToolRefused(f"Step {i}: to {step['do']}, call browser__{step['do']}.")
        if not isinstance(step, dict) or step.get("do") not in DOES:
            raise ToolRefused(f"Step {i}: do is one of {', '.join(DOES)}.")
        do, text = step["do"], str(step.get("text") or "")
        sent: dict = {"do": do}
        if do == "locate":
            then = step.get("then") or "click"
            if not text.strip() or then not in THEN:
                raise ToolRefused(f"Step {i}: locate needs text, what to find in words, and then "
                              f"is click, hover or none.")
            out.append({"do": do, "text": text.strip(), "then": then})
            continue
        if step.get("target") not in (None, ""):
            sent["target"] = target_of(step["target"])
        elif do in NEEDS_TARGET:
            raise ToolRefused(f"Step {i}: {do} needs target, an id from the page like e14.")
        if do in NEEDS_TEXT and not text:
            raise ToolRefused(f"Step {i}: {do} needs text.")
        if do == "type" and step.get("mode") in ("replace", "append"):
            sent["mode"] = step["mode"]
        if do == "press":
            sent["text"] = key_names(text)
        elif text:
            sent["text"] = text
        if do == "restart" and i != len(steps):
            raise ToolRefused(f"Step {i}: {do} ends the call, so it is the last step.")
        out.append(sent)
    return out


class Browser:
    name = "browser"
    guide = guide("browser", max_tabs=MAX_TABS)
    description = described()

    app = None      # the running app (core/services.py), from skills.build

    @property
    def llm(self):
        return self.app.llm if self.app is not None else None

    def __init__(self, cfg):
        self.cfg = cfg
        # one call at a time per tab: its calls of one reply on a tab run in order
        # instead of the box refusing the second as busy
        self._locks: defaultdict[str, anyio.Lock] = defaultdict(anyio.Lock)

    @property
    def status(self) -> str:
        return "up" if sync.url(self.cfg) else "down"

    @property
    def error(self) -> str | None:
        if not sync.url(self.cfg):
            return not_set_up("the browser")
        return None

    async def prompt_fragment(self, ctx: ToolContext) -> str | None:
        """This conversation's own tab, named as its terminal (terminals/store.py)."""
        if ctx.store is None:
            return None
        from ..terminals.store import default_terminal
        name = await default_terminal(ctx.store, ctx.conversation_id)
        return f"Your browser tab in this conversation is `{name}`."

    # ---- the tools ------------------------------------------------------------------------

    @tool(read_only=False, destructive=False, open_world=True)
    async def open(
        self,
        ctx: ToolContext,
        url: Annotated[str, "The address, like https://www.example.com/jobs."],
        tab: Annotated[str, "Which tab, by name. A new name opens a new tab "
                            f"({MAX_TABS} at most)."],
        profile: Annotated[Literal["main", "throwaway"] | None,
                           "For a new tab: main, your cookies and logins (the default), or "
                           "throwaway, a fresh profile that keeps nothing and is gone when "
                           "the tab closes. An open tab keeps its own."] = None,
        wait: Annotated[str, "Words to wait for on the page before answering, for a page "
                             "that fills itself after loading, like \"Comments\"."] = "",
        show: Annotated[SHOW, SHOW_HELP] = "all",
    ) -> ToolResult:
        """Open a web page in your own browser, a real Brave that keeps your cookies and
        runs the page's scripts. For reading an article or a PDF, web__read_page is faster.
        Returns: the page: tab, address, title, where the view is, then the outline of
        what is in view, with an id on everything you can act on."""
        try:
            name, target = tab_name(tab), address(url)
            if why := await refusal(target):
                raise ToolRefused(why)
        except ToolRefused as e:
            return self._error(str(e), tab)
        steps = [{"do": "open", "url": target}]
        if wait.strip():
            steps.append({"do": "wait", "text": wait.strip()})
        before = await self._tab_names()
        out = await self._steps(ctx, name, steps, profile=profile, show=show)
        if before is not None and name not in before and not out.is_error \
                and (now := await self._tab_names()):
            out.content += f"\nOpen tabs: {', '.join(now)} ({MAX_TABS} at most)."
        return out

    async def _tab_names(self) -> list[str] | None:
        """The names of the open tabs, None when the box does not say."""
        try:
            return [str(t.get("name")) for t in (await sync.status(self.cfg)).get("tabs") or []]
        except (httpx.HTTPError, httpx.InvalidURL, BrowserRefused):
            return None

    @tool(read_only=False, open_world=True)
    async def act(
        self,
        ctx: ToolContext,
        steps: Annotated[list[Step], "The steps, run in order; the page comes back after "
                                     "the last one. One step is fine."],
        tab: Annotated[str, "Which tab, by name. A new name opens a new tab "
                            f"({MAX_TABS} at most)."],
        show: Annotated[SHOW, SHOW_HELP] = "all",
    ) -> ToolResult:
        """Act in a browser tab, like a person at the screen, one or more steps in one
        call: click, type (into a target: its text is replaced, or appended), select an
        option, hover, press keys and chords, find words on the page, wait for words,
        back, forward, reload, upload files, accept or dismiss a dialog, restart the
        browser, locate what you name on the screen and click it. To scroll or close a
        tab: browser__scroll, browser__close.
        Example: [{"do": "type", "target": "e5", "text": "python\\n"}].
        Returns: the page as it is afterwards, as browser__open does."""
        try:
            name, sent = tab_name(tab), steps_of(steps)
        except ToolRefused as e:
            return self._error(str(e), tab)
        if sent[-1]["do"] == "restart":
            if len(sent) > 1:
                return self._error("restart goes alone in its call.", tab)
            return await self._restart(name)
        try:
            filled, said = await self._filled(ctx, sent)
        except EntryError as e:
            return self._error(f"Nothing was typed. {e}", tab)
        except VaultError as e:
            return self._error(f"Nothing was typed: {reason(e)}, tell {self.cfg.user}.", tab)
        body = {"tab": name, "steps": filled, **({"show": show} if show != "all" else {})}
        post = self._locating(ctx, name) if any(s["do"] == "locate" for s in sent) else None
        return await self._call(name, body, ctx=ctx, said=said, post=post)

    def _locating(self, ctx: ToolContext, name: str):
        """sync.post for steps with a locate step: each locate is found on a shot of the
        view as it is when its turn comes (vision.py, a box), and becomes a click or hover
        at its centre (a point target) or, with then none, a line alone. The steps between
        two locates go to the box together; the result reads as one call's: an effect per
        step, done and refusals counted over all of them."""
        async def post(body: dict) -> dict:
            steps, effects, res = body["steps"], [], None
            i = 0
            while i < len(steps):
                j = next((k for k in range(i + 1, len(steps))
                          if steps[k]["do"] == "locate"), len(steps))
                chunk, located, start = list(steps[i:j]), None, i
                if chunk[0]["do"] == "locate":
                    try:
                        res, located, _ = await self._locate(ctx, name, chunk[0]["text"])
                    except BrowserRefused as e:
                        raise BrowserRefused(e.status, {**e.facts, "done": i,
                                                        "effects": effects}) from None
                    point, box = located
                    if chunk[0]["then"] == "none":
                        effects.append({"effect": "located", "point": point, "box": box})
                        chunk, start = chunk[1:], i + 1
                    else:
                        chunk[0] = {"do": chunk[0]["then"], "target": f"{point[0]},{point[1]}"}
                if chunk:
                    def ran(facts: dict) -> list[dict]:
                        """The chunk's effects, the located click's with where it was."""
                        got = list(facts.get("effects") or [])
                        if located and start == i and got:
                            got[0] = {**got[0], "point": located[0], "box": located[1]}
                        return got
                    part = {**body, "steps": chunk, **({"show": "none"} if j < len(steps) else {})}
                    try:
                        res = await sync.post(self.cfg, part)
                    except BrowserRefused as e:
                        raise BrowserRefused(e.status, {
                            **e.facts, "done": start + int(e.facts.get("done") or 0),
                            "effects": effects + ran(e.facts)}) from None
                    effects += ran(res)
                i = j
            out = {**res, "effects": effects, "done": len(steps)}
            if body.get("show") == "none":      # a locate came last: its shot's outline
                for k in ("lines", "view", "scrolls"):
                    out.pop(k, None)
            return out
        return post

    async def _locate(self, ctx: ToolContext, name: str, text: str, screen: int | None = None,
                      wait: str = "") -> tuple[dict, tuple[list[int], list[int]], str]:
        """Where `text` is on the view (scrolled to `screen` first, after `wait`'s words):
        (the shot's facts, (centre, box) in px, the model that answered), from the Screen
        reading model's box on a plain shot. BrowserRefused not_located (with the model's
        words) or no_screen (why no model answered)."""
        res = await sync.do(self.cfg, name, "look", what="shot", shot="plain",
                            **({"screen": int(screen)} if screen else {}),
                            **({"wait": wait} if wait else {}))
        try:
            got = await vision.ask(self.llm, base64.b64decode(res["shot"]), text, "box",
                                   conversation_id=ctx.conversation_id, turn_id=ctx.turn_id)
        except vision.Unavailable as e:
            raise BrowserRefused(503, {"error": "no_screen", "why": str(e)}) from None
        if not got.value:
            raise BrowserRefused(422, {"error": "not_located", "text": text, "said": got.said})
        return res, in_view(got.value), got.model

    async def _filled(self, ctx: ToolContext | None,
                      sent: list[dict]) -> tuple[list[dict], list[dict]]:
        """Every vault entry in a type step's text filled in, before anything is sent:
        that step is secret (browserd: no draft, no shot) and carries the entry's sites,
        and browserd types it only into a page of one of them; that is its guard, no tap.
        -> (the
        steps to send, the steps as it wrote them with each placeholder naming the
        entry it matched). VaultError or EntryError when one cannot be."""
        filled, said = [], []
        for step in sent:
            m = VAULT.fullmatch(step.get("text", "")) if step["do"] == "type" else None
            if m:
                it, pw = await self.app.vault.secret(m[1], m[2])
                entry, sites = it["name"], vault_mod.origins(it)
                said.append({**step, "text": f"vault:{entry}/{m[2]}{m[3]}"})
                step = {**step, "text": pw + m[3], "secret": True, "sites": sites}
            else:
                said.append(step)
            filled.append(step)
        return filled, said

    @tool(read_only=True, open_world=True)       # the page changes nothing: reading may scroll
    async def scroll(
        self,
        ctx: ToolContext,
        tab: Annotated[str, "Which tab, by name. A new name opens a new tab "
                            f"({MAX_TABS} at most)."],
        direction: Annotated[Literal["down", "up", "top", "bottom"],
                             "down or up, most of a screen; top or bottom of the page."] = "down",
        to: Annotated[str | None, "An id from the page: scroll it into view instead, or, "
                                  "with amount, turn the wheel inside it (a scrollarea)."] = None,
        amount: Annotated[int | None, "Pixels instead of a screen, like 300 or -1200."] = None,
        show: Annotated[SHOW, SHOW_HELP] = "all",
    ) -> ToolResult:
        """Scroll a browser tab: most of a screen down or up, to the top or bottom, by
        pixels, or an element into view. Returns: the page as browser__open does."""
        try:
            name = tab_name(tab)
            step: dict = {"do": "scroll"}
            if to not in (None, ""):
                step["target"] = target_of(to)
                if direction in ("top", "bottom") and not amount:
                    raise ToolRefused(TOP_WITH_TARGET)
            if amount:
                step["text"] = str(int(amount))
            elif to in (None, "") or direction != "down":
                step["text"] = direction
        except (ToolRefused, ValueError) as e:
            return self._error(str(e) or "amount is a number of pixels.", tab)
        return await self._steps(ctx, name, [step], show=show)

    @tool(read_only=False, destructive=False, open_world=True)
    async def close(
        self,
        ctx: ToolContext,
        tab: Annotated[str, "Which tab, by name. A new name opens a new tab "
                            f"({MAX_TABS} at most)."],
    ) -> ToolResult:
        """Close a browser tab. Returns: the tabs still open. Leave a tab open while you
        use it; closing frees room for another."""
        try:
            name = tab_name(tab)
        except ToolRefused as e:
            return self._error(str(e), tab)
        return await self._steps(ctx, name, [{"do": "close"}])

    @tool(read_only=True, open_world=True)
    async def look(
        self,
        ctx: ToolContext,
        tab: Annotated[str, "Which tab, by name. A new name opens a new tab "
                            f"({MAX_TABS} at most)."],
        what: Annotated[Literal["view", "page", "html", "source", "screen", "locate"],
                        "view: the outline of what is in view, as after a step. page: the "
                        "whole page as text, in parts. html: its HTML as it is now, in "
                        "parts. source: the document as the server sent it, like a JSON "
                        "answer, in parts. screen: the screen itself. locate: where what "
                        "the question names is on the screen, as a point to "
                        "click."] = "view",
        target: Annotated[str | None, "view: an id; the outline of that element alone, "
                                      "the whole of it, like a comment thread or a "
                                      "form."] = None,
        find: Annotated[str, "view: words, or /a regex/, anywhere on the page; the "
                             "matching lines with a line around each. page, html and "
                             "source: the part that has these words."] = "",
        part: Annotated[int | None, "page, html and source: which part; screen and "
                                    "locate: which screen of the page, as the screen line "
                                    "counts them (the view scrolls there first). From "
                                    "1."] = None,
        wait: Annotated[str, "Words to wait for on the page first, like \"Comments\"."] = "",
        question: Annotated[str, "screen: a question about it, for a model that sees it, "
                                 "like \"What is the score?\". When your own model sees "
                                 "images you get the screen itself and need none. locate: "
                                 "what to find, like \"the Play button\"."] = "",
        answer_as: Annotated[Literal["text", "id"],
                             "screen, with a question: text, a few sentences; id, the id "
                             "of the element asked about."] = "text",
    ) -> ToolResult:
        """Read a browser tab without changing it: the view again, the whole page as
        clean text (a thread with its comments, an article, a README), its HTML, or the
        screen, for what the text does not tell: pictures, a canvas, where something is.
        Returns: the tab, address and title, then what was asked for."""
        try:
            name = tab_name(tab)
        except ToolRefused as e:
            return self._error(str(e), tab)
        body: dict = {"tab": name, "do": "look", "what": what}
        if wait.strip():
            body["wait"] = wait.strip()
        if what == "screen":
            return await self._screen(ctx, name, question.strip(), answer_as, part,
                                      wait.strip())
        if what == "locate":
            return await self._located(ctx, name, question.strip(), part, wait.strip())
        if what == "view":
            if find.strip():
                body["find"] = find.strip()
            elif target not in (None, ""):
                try:
                    body["target"] = target_of(target)
                except ToolRefused as e:
                    return self._error(str(e), tab)
            return await self._call(name, body, ctx=ctx)
        try:
            async with self._locks[name]:
                res = await sync.post(self.cfg, body)
        except BrowserRefused as e:
            return self._refusal(e, name)
        except httpx.HTTPError as e:
            return self._unreachable(name, e)
        if what == "source" and "binary" in res:
            return ToolResult(content="\n".join([self._head(res), f"source: not text, "
                                                 f"{res['binary']} bytes. Its address "
                                                 f"downloads it."] + self._notes(res)),
                              display=self._display(res))
        text = res.get({"page": "markdown", "html": "html", "source": "source"}[what]) or ""
        budget = PART_TOKENS
        if ctx.max_output_tokens:
            budget = max(800, min(budget, int(ctx.max_output_tokens) - 300))
        parts = split_parts(text.strip(), budget) if text.strip() else [""]
        k = max(1, min(int(part or 1), len(parts)))
        lines = [self._head(res)]
        if find.strip():
            want = find.strip().lower()
            hit = next((i for i, p in enumerate(parts, 1) if want in p.lower()), None)
            if hit is None:
                lines.append(f"\"{find.strip()}\" is not on this page.")
                return ToolResult(content="\n".join(lines + self._notes(res)),
                                  display=self._display(res))
            k = hit
        kind = {"page": "text", "html": "HTML", "source": "source"}[what]
        lines.append(f"{kind}, part {k} of {len(parts)}" if len(parts) > 1 else kind)
        lines += ["", parts[k - 1] or "(nothing readable on this page)", ""]
        if len(parts) > 1:
            nav = []
            if k > 1:
                nav.append(f"earlier: browser__look(what=\"{what}\", part={k - 1}, "
                           f"tab=\"{name}\")")
            nav.append(f"later: browser__look(what=\"{what}\", part={k + 1}, tab=\"{name}\")"
                       if k < len(parts) else "end of page")
            lines.append(", ".join(nav))
        return ToolResult(content="\n".join(lines + self._notes(res)),
                          display={**self._display(res), "what": what, "part": k,
                                   "parts": len(parts)})

    # ---- calls ----------------------------------------------------------------------------

    async def _steps(self, ctx: ToolContext, name: str, steps: list[dict],
                     profile: str | None = None, show: str = "all") -> ToolResult:
        body = {"tab": name, "steps": steps}
        if profile:
            body["profile"] = profile
        if show != "all":
            body["show"] = show
        return await self._call(name, body, ctx=ctx)

    async def _call(self, name: str, body: dict, ctx: ToolContext | None = None,
                    said: list[dict] | None = None, post=None) -> ToolResult:
        """`said`: the steps as it wrote them, a vault entry as its placeholder; every
        word of the result is composed from these, never from what was sent. `post`:
        _locating's for steps with a locate step, else sync.post."""
        steps = said or body.get("steps") or []
        try:
            async with self._locks[name]:
                res = await post(body) if post else await sync.post(self.cfg, body)
        except BrowserRefused as e:
            out = self._refusal(e, name, steps)
            done = e.facts.get("effects") or []
            if done:
                out.content += "\n" + "\n".join(self._effects(steps, done))
                out.display["steps"] = await self._shots(ctx, name, steps, done)
            return out
        except httpx.HTTPError as e:
            return self._unreachable(name, e)
        if "closed" in res:
            left = res["tabs"]
            lines = [f"Closed tab {name}. " + (f"Open tabs: {', '.join(left)}." if left
                                               else "No tab is open.")]
            return ToolResult(content="\n".join(lines + self._notes(res)),
                              display={"tab": name, "closed": True})
        budget = OUTLINE_TOKENS
        if ctx is not None and ctx.max_output_tokens:
            budget = min(budget, int(ctx.max_output_tokens))
        display = self._display(res)
        if res.get("effects"):
            display["steps"] = await self._shots(ctx, name, steps, res["effects"])
        content = self._page(res, budget, {**body, "steps": steps})
        if any(PASSWORD_FIELD.match(ln) for ln in res.get("lines") or []):
            content += "\nA password field: vault__get or vault__set."
        return ToolResult(content=content, display=display)

    async def _shots(self, ctx: ToolContext | None, name: str, steps: list[dict],
                     effects: list[dict]) -> list[dict]:
        """Each step for the user's row: its words, its effect line, the view after it
        (kept with the call, never sent to it; a vault entry typed has none)."""
        out = []
        for i, (step, eff) in enumerate(zip(steps, effects), 1):
            words = self._step_words(step)
            if step["do"] == "type" and VAULT.fullmatch(step.get("text", "")):
                words = f"Typed {step['text'].strip()} {into(step.get('target'))}"
            row = {"step": words, "effect": self._effect(step, eff)}
            if eff.get("shot") and ctx is not None:
                row["shot"] = await self._keep(ctx, base64.b64decode(eff["shot"]),
                                               f"{name}-step{i}.jpg", "image/jpeg")
            out.append(row)
        return out

    async def _screen(self, ctx: ToolContext, name: str, question: str,
                      answer_as: str, screen: int | None = None, wait: str = "") -> ToolResult:
        """The screen: to it as an image when its model sees images (with marks when
        it asked for ids), else a model of the Screen reading role answers its question
        (vision.py)."""
        sees = await attachments.turn_sees_images(self.cfg, ctx)
        if not sees and not question:
            if not await self._screen_model():
                return self._error(f"You cannot see the screen: your model does not take "
                                   f"images and no model is set for screen reading. Tell "
                                   f"{self.cfg.user}. browser__look(tab=\"{name}\", "
                                   f"what=\"view\") and what=\"page\" still work.", name)
            return self._error(f"Your model does not take images, so ask a question about "
                               f"the screen: browser__look(tab=\"{name}\", what=\"screen\", "
                               f"question=\"...\").", name)
        kind = SHOTS[answer_as] if question or answer_as != "text" else "plain"
        try:
            async with self._locks[name]:
                res = await sync.do(self.cfg, name, "look", what="shot", shot=kind,
                                    **({"screen": int(screen)} if screen else {}),
                                    **({"wait": wait} if wait else {}))
        except BrowserRefused as e:
            return self._refusal(e, name)
        except httpx.HTTPError as e:
            return self._unreachable(name, e)
        png = base64.b64decode(res["shot"])
        display = {**self._display(res),
                   "shot": await self._keep(ctx, png, f"{name}-{kind}.png", "image/png")}
        if sees:
            lines = [self._head(res), *([self._screens(res["view"])] if res.get("view") else []),
                     f"The screen is in the image, {VIEW[0]}x{VIEW[1]} px" + (
                     " (the pink numbers are the ids below)." if kind == "marks" else
                     f": a click on a point of it is the target x,y in px, like "
                     f"{VIEW[0] // 2},{VIEW[1] // 2}.")]
            if kind == "marks":
                lines += res.get("lines") or []
            return ToolResult(content="\n".join(lines + self._notes(res)),
                              display={**display, "see": True})
        turn = {"conversation_id": ctx.conversation_id, "turn_id": ctx.turn_id}
        try:
            got = await vision.ask(self.llm, png, question, answer_as, **turn)
        except vision.Unavailable as e:
            return self._error(self._blind(e, name), name)
        display.update(question=question, model=got.model)
        answer = got.value
        if answer_as == "id":
            n = int(answer[1:]) if answer else None
            line = next((ln for ln in res.get("lines") or []
                         if re.search(rf"\be{n}\b", ln)), None) if n else None
            answer = line.strip("- ") if line else None
        if answer is None:
            answer = "none" + (f" (the model said: \"{got.said}\")"
                               if got.said.strip(" .").lower() not in ("", "none") else "")
        display["answer"] = answer
        return ToolResult(content="\n".join([self._head(res), f"answer: {answer}"]
                                            + self._notes(res)), display=display)

    async def _located(self, ctx: ToolContext, name: str, question: str,
                       screen: int | None = None, wait: str = "") -> ToolResult:
        """Where something is on the screen, by the Screen reading model's box: the box
        and its centre in px of the view, ready as a click's target, and nothing clicked."""
        if not question:
            return self._error("locate needs a question, what to find in words, like "
                               "question=\"the blue Sign in button\".", name)
        try:
            async with self._locks[name]:
                res, (point, box), model = await self._locate(ctx, name, question, screen,
                                                              wait)
        except BrowserRefused as e:
            return self._refusal(e, name)
        except httpx.HTTPError as e:
            return self._unreachable(name, e)
        x, y = point
        shot = await self._keep(ctx, base64.b64decode(res["shot"]), f"{name}-plain.png",
                                "image/png")
        answer = (f"found at {x},{y}, box {box[0]},{box[1]} to {box[2]},{box[3]} (a click "
                  f"there: {{\"do\": \"click\", \"target\": \"{x},{y}\"}})")
        return ToolResult(content="\n".join([self._head(res), answer] + self._notes(res)),
                          display={**self._display(res), "shot": shot, "question": question,
                                   "model": model, "point": point, "box": box,
                                   "answer": answer})

    def _blind(self, why, tab: str) -> str:
        return (f"The question was not asked: {why} Tell {self.cfg.user}. "
                f"browser__look(tab=\"{tab}\", what=\"view\") and what=\"page\" still "
                f"work.")

    async def _screen_model(self) -> bool:
        """Whether a model of the Screen reading role can answer now."""
        return self.llm is not None and bool((await self.llm.router.order("screen")).refs)

    async def _keep(self, ctx: ToolContext, image: bytes, filename: str,
                    media_type: str) -> dict:
        """A shot of the view, kept with the call alone: its row in the chat shows it, and
        the context builder hands it to it when its model sees images and the result
        says so (display "see"). Without a call (the user's console), a draft the next
        cleanup removes."""
        row = await attachments.save_generated(self.cfg, ctx.store, image, filename,
                                               media_type, ctx.tool_call_id or None,
                                               in_message=False)
        return {**{k: row[k] for k in ("id", "sha256", "filename", "media_type", "bytes")},
                "width": VIEW[0], "height": VIEW[1]}

    async def _restart(self, name: str) -> ToolResult:
        try:
            now = await sync.restart(self.cfg, name)
        except BrowserRefused as e:
            return self._refusal(e, name)
        except httpx.HTTPError as e:
            return self._unreachable(name, e)
        if now["state"] == "down":
            return self._error(f"The browser restarted but could not start: "
                               f"{self._reason(now.get('reason'))}. Tell {self.cfg.user}.", name)
        if now["state"] != "up":
            return ToolResult(content="The browser is still starting; your next call tells "
                                      "when it is back.", display={"restart": True})
        tabs = [f"{t['name']}  {t['url']}" for t in now["tabs"]]
        return ToolResult(content="\n".join(["The browser restarted. "
                                             + ("Its tabs reopened:" if tabs else
                                                "No tab is open.")] + tabs),
                          display={"restart": True})

    # ---- composing its result -------------------------------------------------------------

    @staticmethod
    def _head(res: dict) -> str:
        line = (("throwaway " if res.get("profile") == "throwaway" else "")
                + f"tab {res['tab']}  {res['url']}  \"{res['title']}\"")
        if res.get("error"):
            line += f"  not loaded: {res['error']}"
        elif res.get("status") and res["status"] >= 400:
            line += f"  HTTP {res['status']}"
        if res.get("loading"):
            line += "  still loading"
        return line

    def _page(self, res: dict, budget: int = OUTLINE_TOKENS, body: dict | None = None) -> str:
        lines = [self._head(res)]
        dialog = res.get("dialog")
        if dialog:
            lines.append(self._dialog(dialog))
        body = body or {}
        lines += self._effects(body.get("steps") or [], res.get("effects") or [],
                               res.get("url"))
        view = res.get("view")
        if view:
            lines.append(self._screens(view))
        outline = res.get("lines")
        if res.get("unchanged"):
            lines.append("The outline is as in your last result.")
        elif "found" in res:
            lines.append(f"{res['found']} lines have \"{body.get('find')}\":" if res["found"]
                         else f"Nothing on the page has \"{body.get('find')}\".")
        elif outline is not None and not outline:
            lines.append(f"Nothing in view. Scroll, or browser__look(what=\"page\", "
                         f"tab=\"{res['tab']}\") reads the page.")
        text, shown, total = tokens.cut_to("\n".join(outline or []), budget)
        if text:
            lines.append(text)
        if shown < total:
            lines.append(f"(the outline goes on: {shown} of {total} tokens shown; scroll, "
                         f"or browser__look(what=\"page\", tab=\"{res['tab']}\") reads the "
                         f"page)")
        for d in res.get("drafts") or []:
            lines.append(f"Text you typed into {d['field']} on {d['url']}, which the tab has "
                         f"left since: \"{d['text']}\"")
        return "\n".join(lines + self._notes(res))

    @staticmethod
    def _screens(view: dict) -> str:
        """Where the view is, in screens (a screen is the view's height; a scroll down
        moves one): screen 2 of 7: 1.4 above, 4.6 below."""
        y, h, page = view["y"], max(1, view["h"]), view["page"]
        if page <= h + 1:
            return "the whole page is in view"
        n = max(1, math.ceil(page / h - 0.01))
        below = max(0.0, (page - y - h) / h)
        k = n if below < 0.01 else min(n, round(y / h) + 1)
        above = "top" if y < 1 else f"{y / h:.1f} above"
        return f"screen {k} of {n}: {above}, " + ("bottom" if below < 0.01 else f"{below:.1f} below")

    def _effects(self, steps: list[dict], effects: list[dict],
                 url: str | None = None) -> list[str]:
        """One line per step run: what it did. `url`: the page's address on the head
        line, so a step that went there says so instead of repeating it (a search
        engine's address runs to a thousand characters)."""
        out = []
        for step, eff in zip(steps, effects):
            line = f"{self._step_words(step)}: {self._effect(step, eff, url)}"
            if eff.get("effect") == "changed":
                line += "".join("\n    " + ln.lstrip("- ") for ln in eff.get("lines") or [])
            out.append(line)
        return out

    @staticmethod
    def _effect(step: dict, eff: dict, url: str | None = None) -> str:
        kind, text = eff.get("effect"), step.get("text", "")
        if step.get("do") == "locate":
            (x, y), b = eff["point"], eff["box"]
            found = f"found at {x},{y} (box {b[0]},{b[1]} to {b[2]},{b[3]})"
            if kind == "located":
                return found
            return (f"{found}, {DID[step['then']]} there: "
                    + Browser._effect({"do": step["then"]}, eff, url))
        if kind == "navigated":
            where = "the address above" if url and eff.get("url") == url else str(eff.get("url"))
            return ("loaded " if step.get("do") == "open" else "went to ") + where
        if kind == "tab":
            return f"the page opened a new tab, and this tab followed it to {eff.get('url')}"
        if kind == "changed":
            return (f"the page changed, {eff.get('added', 0)} lines new, "
                    f"{eff.get('gone', 0)} gone" + (":" if eff.get("lines") else ""))
        if kind == "focus":
            return f"only the focus moved, to {str(eff.get('to', '')).lstrip('- ')}"
        if kind == "dialog":
            return "a dialog opened"
        if kind == "download":
            return f"a download began: {eff.get('file')}"
        if kind == "scrolled":
            return "scrolled"
        if kind == "found":
            return f"\"{text}\" is in view now"
        if kind == "not_found":
            return f"\"{text}\" is not on this page"
        if kind == "waited":
            return f"\"{text}\" is on the page"
        if kind == "not_waited":
            return f"\"{text}\" did not appear within 10 s"
        if step.get("do") == "scroll":
            return "the view did not move"
        return "nothing changed on the page"

    @staticmethod
    def _step_words(step: dict) -> str:
        """The step as it wrote it, on one line: a newline it typed (Enter) reads \\n."""
        return " ".join(str(step.get(k)) for k in ("do", "target", "text")
                        if step.get(k)).replace("\n", "\\n")

    @staticmethod
    def _dialog(dialog: dict) -> str:
        return (f"A {dialog['type']} dialog asks: \"{dialog['message']}\". A step "
                f"{{\"do\": \"accept\"}} or {{\"do\": \"dismiss\"}} answers it; until then "
                f"nothing else works on this tab.")

    def _notes(self, res: dict) -> list[str]:
        return [f"note: {self._note(str(n))}" for n in res.get("notes") or []]

    def _note(self, code: str) -> str:
        key, _, rest = code.partition(":")
        if key == "restarted":
            at = self._at(rest)
            return (f"The browser restarted at {at} after a crash or a hang. Your tabs "
                    f"reopened at their last addresses; a login from the last minute may be "
                    f"gone.")
        if key == "taken":
            since, _, until = rest.partition(":")
            return (f"{self.cfg.user} used the browser from {self._at(since)} to "
                    f"{self._at(until)}.")
        if key == "crashed":
            tab, _, status = rest.partition(":")
            return (f"Tab {tab} crashed ({CRASHES.get(status, status)}) and was loaded "
                    f"again.")
        if key == "throwaway":
            return (f"Tab {rest} was a throwaway tab: it closed when the browser restarted, "
                    f"and its cookies are gone.")
        if key == "extensions_off":
            return ("The browser did not start with your extensions in ~/.browser-extensions, "
                    "so it runs without them. Most likely one of them is broken: fix or remove "
                    "it, then restart the browser.")
        if key == "download":
            return f"Downloaded ~/Downloads/{rest}; your shellbox sees it there too."
        if key == "download_failed":
            return f"The download of {rest} failed."
        if key == "waited":
            return f"\"{rest}\" is on the page."
        if key == "not_waited":
            return f"\"{rest}\" did not appear within 10 s; this is the page as it is."
        if key == "closed":
            name, _, url = rest.partition(":")
            return f"Tab {name} was closed to make room for the new tab; it was at {url}."
        if key == "beforeunload":
            return ("The page asked before being left; it stayed, since you had typed on it. "
                    "A step {\"do\": \"reload\"} or a new address leaves it anyway."
                    if rest == "stayed" else "The page asked before being left and was left.")
        return f"The browser noted: {code}."

    def _at(self, epoch) -> str:
        """A time from the box, HH:MM in the app's zone."""
        try:
            return datetime.fromtimestamp(float(epoch), zone(self.cfg)).strftime("%H:%M")
        except (TypeError, ValueError, OverflowError, OSError):
            return "just now"

    def _reason(self, reason: str | None) -> str:
        reason = str(reason or "")
        if reason.startswith("exited:"):
            return f"Brave exited at once (exit {reason[7:]})"
        return LAUNCH.get(reason, reason or "no reason given")

    @staticmethod
    def _display(res: dict) -> dict:
        return {"tab": res.get("tab"), "url": res.get("url"), "title": res.get("title")}

    @staticmethod
    def _error(text: str, tab) -> ToolResult:
        return ToolResult(content=text, is_error=True, display={"tab": tab, "refused": True})

    def _unreachable(self, tab: str | None, e: Exception) -> ToolResult:
        return self._error(f"{unreachable_words('the browser', sync.url(self.cfg), e)} Tell "
                           f"{self.cfg.user} if it stays away.", tab)

    def _refusal(self, e: BrowserRefused, tab: str | None, steps: list | None = None) -> ToolResult:
        f, code, user = e.facts, e.facts.get("error"), self.cfg.user
        done = f.get("done")
        step = steps[done] if steps and isinstance(done, int) and done < len(steps) else None
        where = ""
        if step and len(steps) > 1:
            where = (f"Step {done + 1} ({self._step_words(step)}) was refused"
                     + (f"; steps 1 to {done} were done" if done else "") + ": ")
        if code == "no_screen":
            return self._error(where + self._blind(f.get("why"), tab), tab)
        if code == "not_located":
            said = f.get("said") or ""
            return self._error(where + f"The screen reading model found no \"{f.get('text')}\" "
                               f"on the screen; nothing was done. " + (
                                   f"It said: \"{said}\"" if said else "It said nothing."), tab)
        if e.status in (401, 403):
            return self._error(f"{bad_key('the browser', sync.url(self.cfg))} Tell {user}.", tab)
        if code == "no_tab":
            open_tabs = f.get("tabs") or []
            return self._error(f"No tab named {tab}. " + (
                f"Open tabs: {', '.join(open_tabs)}." if open_tabs else
                "No tab is open; browser__open opens one."), tab)
        if code == "tabs_full":
            return self._error(f"There are already {MAX_TABS} tabs open, each with text you "
                               f"typed on its page: {', '.join(f.get('tabs') or [])}. Close "
                               f"one first: browser__close(tab=\"<name>\").", tab)
        if code == "busy":
            return self._error(f"Tab {tab} is busy with another call.", tab)
        if code == "taken":
            return self._error(f"{user} took over the browser at {self._at(f.get('since'))}; "
                               f"nothing was done.", tab)
        if code == "dialog":
            return self._error(where + f"{self._dialog(f['dialog'])}", tab)
        if code == "profile":
            return self._error(f"Tab {tab} is a {f.get('profile')} tab and keeps that profile; "
                               f"nothing was done. Open it without profile, or use a new tab "
                               f"name.", tab)
        if code == "id_gone":
            target = (step or {}).get("target", "")
            return self._error(where + f"{target} is no longer on the page. The page now:\n"
                               + self._page(f), tab)
        if code in ("covered", "disabled", "moving"):
            target = (step or {}).get("target") or (           # a locate step: its words
                f"\"{step['text']}\"" if step and step["do"] == "locate" else "")
            by = f.get("by") or {}
            what = " ".join(x for x in (by.get("role"), f'"{by["name"]}"' if by.get("name")
                                        else "", by.get("id")) if x)
            return self._error(where + {
                "covered": f"{target} is covered by {what}: nothing was done. Answer or "
                           f"close what is on top first.",
                "disabled": f"{target} is disabled: nothing was done.",
                "moving": f"{target} kept moving for a second: nothing was done."}[code], tab)
        if code == "no_source":
            return self._error(f"Tab {tab} has no source to show: the page is not loaded "
                               f"from an address.", tab)
        if code == "bad" and f.get("what") == "screen":
            return self._error(f"The page has {f.get('screens')} screens; part is 1 to "
                               f"{f.get('screens')}.", tab)
        if code == "bad" and f.get("what") == "target":
            target = (step or {}).get("target", "")
            known = f.get("ids") or []
            return self._error(where + f"{target} is not an id of this page. " + (
                f"Its ids are e{min(known)} to e{max(known)}; use the latest result." if known
                else f"It has no ids yet; browser__look(what=\"view\", tab=\"{tab}\") "
                     f"shows them."), tab)
        if code == "no_select":
            target = (step or {}).get("target", "")
            return self._error(where + f"{target} is a {f.get('role')}, not a select: click "
                               f"it and click the option.", tab)
        if code == "no_option":
            opts = ", ".join(f.get("options") or [])
            return self._error(where + f"No such option. The options: {opts or 'none'}.", tab)
        if code == "no_file":
            return self._error(where + f"There is no file {f.get('file')}.", tab)
        if code == "wrong_site":
            return self._error(f"Nothing was typed {into(f.get('target'))}. That page is not "
                               f"one of the entry's own sites, so the vault does not type it "
                               f"there; {self.cfg.user} is told.", tab)
        if code == "password_field":
            return self._error(f"Nothing was typed {into(f.get('target'))}. A password field "
                               f"takes a vault entry: vault__set(name) makes one, "
                               f"vault__get(name) gives the text to type.", tab)
        if code == "not_password":
            return self._error(f"Nothing was typed {into(f.get('target'))}. A vault entry is "
                               f"typed only into a password field.", tab)
        if code == "no_back":
            return self._error(where + f"There is no earlier page in tab {tab}.", tab)
        if code == "no_forward":
            return self._error(where + f"There is no later page in tab {tab}.", tab)
        if code == "no_dialog":
            return self._error(where + f"No dialog is open on tab {tab}.", tab)
        if code == "bad":
            return self._error(where + f"The browser box did not take {f.get('what')}.", tab)
        if code == "starting":
            return self._error("The browser is starting.", tab)
        if code == "down":
            return self._error(f"The browser is down: {self._reason(f.get('reason'))}.", tab)
        if code == "gone":
            return self._error("The browser went away during this call (a crash or a hang) and "
                               "is restarting. Whether this call happened is unknown; your next "
                               "result says when it is back.", tab)
        if code == "no_answer":
            return self._error(f"Tab {tab} did not answer within 10 s; a script may hang the "
                               f"page. It was at {f.get('url')}. Close the tab, or open the "
                               f"page again.", tab)
        if code == "cdp":
            return self._error(where + f"The browser could not do it: {f.get('message')}.", tab)
        return self._error(f"The browser box answered {e.status}: {code or 'no reason'}.", tab)
