"""The page as an outline it can reason over, from Chrome's accessibility tree and one
DOM snapshot, with no script in the page. Pure functions: browserd feeds it CDP answers
and a hit test, tests feed it saved ones.

The view (outline): one line per node that matters, indented by structure, in the
page's own order:

    - heading "json, JSON encoder and decoder" [level=1]
    - text: JSON is a lightweight data interchange format inspired by JavaScript[e14].
    - link "Source code" e15
    - textbox "Search" e3: python [active]
    - password field "Password" e4
    - combobox "Python version" e7: 3.14 (options: dev (3.16), pre (3.15), 3.14, 3.13)
    - checkbox "Bacon" e9 [checked]
    - frame "Sign in with Google":
      - button "Sign in" e21

An id, e<n>, marks what it can act on: it stays the same for that element as long as
the page lives (it is the element's backend node id, numbered per tab), so a second
snapshot keeps the ids of the first. A link inside a sentence is written inline as
name[e14]. [new] marks an element the tab had not seen before, [active] the focused one.
Only what is in view and not covered (a cookie wall hides what is under it) is shown.

The page (reading): the same tree as Markdown, the whole page, no ids, no buttons,
navigation and footers left out; shadow DOM and frames are in, as rendered.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

# roles it acts on: a line of their own with an id
INTERACTIVE = {"button", "link", "textbox", "searchbox", "combobox", "listbox", "checkbox",
               "radio", "switch", "slider", "spinbutton", "menuitem", "menuitemcheckbox",
               "menuitemradio", "tab", "option", "treeitem", "DisclosureTriangle",
               "InputTime", "ColorWell", "DateTime", "Date", "Time", "Week", "Month",
               "scrollbar"}
# interactive roles whose children are read on (the rest name themselves)
COMPOSITE = {"menu", "menubar", "tablist", "tree", "treeitem", "radiogroup", "grid",
             "menuitem", "tab"}
SELECTS = {"combobox", "listbox"}      # a <select> lists its options inline, chosen by text
# structure it sees as a line with children under it
STRUCTURE = {"navigation", "banner", "contentinfo", "complementary", "main", "form",
             "dialog", "alertdialog", "region", "search", "article", "table", "row",
             "list", "figure", "tablist", "menu", "menubar", "tree", "radiogroup",
             "blockquote", "DescriptionList", "toolbar", "feed"}
CHROME = {"navigation", "banner", "contentinfo", "complementary", "search", "toolbar"}
# one line, its name is its text
LEAVES = {"heading", "image", "separator", "Video", "Audio", "PluginObject", "math",
          "progressbar", "meter", "Figcaption"}
# blocks: their inline content becomes one text line; nested blocks go under it
BLOCKS = {"paragraph", "listitem", "cell", "columnheader", "rowheader", "term",
          "definition", "Legend", "caption", "code", "Pre", "LabelText", "note",
          "status", "alert", "log", "marquee", "timer", "tooltip", "sectionheader",
          "sectionfooter", "section", "doc-subtitle", "time"}
SKIP = {"InlineTextBox", "ListMarker", "LineBreak"}
# Chrome's own role names in plain words
WORDS = {"DisclosureTriangle": "details", "InputTime": "time", "ColorWell": "color",
         "DateTime": "datetime", "DescriptionList": "list", "Figcaption": "caption",
         "PluginObject": "plugin", "LabelText": "label", "Legend": "legend",
         "Video": "video", "Audio": "audio", "Pre": "code", "RootWebArea": "page"}
STATES = ("checked", "disabled", "expanded", "pressed", "selected", "required",
          "readonly", "invalid", "multiline")
RUN_MAX = 300           # characters of one text run in the view
NAME_MAX = 120          # characters of a name
OPTIONS_MAX = 12        # options listed with one combobox or listbox
ICON_MAX = 24           # px: an image smaller than this is an icon, not shown
WORDY = re.compile(r"[^\W_]")   # a run worth a line has a letter or a digit


@dataclass
class Frame:
    """One document: its AX nodes, the DOM snapshot document that holds its geometry
    (page coordinates after `offset`), the session a click on it goes to, and the
    frames inside it by the backend node id of their <iframe>."""
    ax: list[dict]
    snap: dict                      # DOMSnapshot.captureSnapshot's answer
    doc: int = 0                    # which of its documents
    offset: tuple[float, float] = (0.0, 0.0)
    session: str = ""
    children: dict[int, "Frame"] = field(default_factory=dict)
    owner: int | None = None        # a frame of another process: its <iframe> in the parent


@dataclass
class View:
    x: float
    y: float
    w: float
    h: float
    page_h: float


@dataclass
class Node:
    """One element it can act on, as the result keeps it."""
    id: int
    session: str
    backend: int
    role: str
    box: tuple[float, float, float, float]      # page coordinates
    frame: Frame
    options: list[str] = field(default_factory=list)
    password: bool = False          # an <input type=password>: it takes a vault entry only


@dataclass
class Item:
    kind: str                       # text, node, block
    text: str = ""                  # the run, or the line without its indent
    kids: list = field(default_factory=list)
    role: str = ""
    name: str = ""
    level: int = 0
    ref: int = 0


@dataclass
class Rendered:
    lines: list[str]
    nodes: dict[int, Node]          # by id: everything with an id in this snapshot
    scrolls: list[tuple[int, int, int]]   # scroll areas in view: id, px below, px above


class Ids:
    """A tab's ids: (session, backend node id) -> e<n>, given once and kept while the
    page lives."""

    def __init__(self):
        self.by_node: dict[tuple[str, int], int] = {}

    def reset(self) -> None:
        self.by_node.clear()

    def get(self, session: str, backend: int) -> int:
        key = (session, backend)
        if key not in self.by_node:
            self.by_node[key] = len(self.by_node) + 1
        return self.by_node[key]


def geometry(frame: Frame) -> tuple[dict, dict, set, dict, set, set]:
    """By backend node id: the box (page coordinates), the parent's backend id, the
    clickable ones, the scroll areas as (scroll rect, client rect), the <select>
    elements and the password fields."""
    doc = frame.snap["documents"][frame.doc]
    nodes, layout, strings = doc["nodes"], doc["layout"], frame.snap["strings"]
    backend = nodes["backendNodeId"]
    selects = {backend[i] for i, name in enumerate(nodes["nodeName"]) if strings[name] == "SELECT"}
    passwords = {backend[i] for i, name in enumerate(nodes["nodeName"])
                 if strings[name] == "INPUT" and is_password(nodes["attributes"][i], strings)}
    dx = frame.offset[0] - doc.get("scrollOffsetX", 0) if frame.offset != (0.0, 0.0) else 0.0
    dy = frame.offset[1] - doc.get("scrollOffsetY", 0) if frame.offset != (0.0, 0.0) else 0.0
    box, scrolls = {}, {}
    for k, i in enumerate(layout["nodeIndex"]):
        x, y, w, h = layout["bounds"][k]
        box.setdefault(backend[i], (x + dx, y + dy, w, h))
        style = layout["styles"][k]
        if style and strings[style[0]] in ("auto", "scroll", "overlay"):
            scrolls.setdefault(backend[i], (layout["scrollRects"][k], layout["clientRects"][k]))
    parent = {backend[i]: backend[p] for i, p in enumerate(nodes["parentIndex"]) if p >= 0}
    clickable = {backend[i] for i in nodes["isClickable"]["index"]}
    return box, parent, clickable, scrolls, selects, passwords


def is_password(attributes: list[int], strings: list[str]) -> bool:
    """An <input type=password>, from its attributes (name, value, name, value...)."""
    pairs = dict(zip(attributes[::2], attributes[1::2]))
    return any(strings[k].lower() == "type" and strings[v].strip().lower() == "password"
               for k, v in pairs.items())


def prop(n: dict, name: str):
    for p in n.get("properties", []):
        if p["name"] == name:
            return p["value"].get("value")
    return None


def value_of(n: dict) -> str:
    v = n.get("value", {}).get("value")
    return "" if v in (None, "") else str(v)


def name_of(n: dict) -> str:
    return " ".join(str(n.get("name", {}).get("value") or "").split())


def cut(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def quote(s: str) -> str:
    return '"' + cut(s, NAME_MAX).replace('"', "'") + '"'


def has_article(item) -> bool:
    return item.role == "article" or any(has_article(k) for k in item.kids)


def has_ref(items: list) -> bool:
    return any(k.kind == "node" and (k.ref or has_ref(k.kids)) or k.kind == "block" and has_ref(k.kids)
               for k in items)


def assemble(ax: list[dict], snap: dict, frames: dict[str, list[dict]], session: str = "") -> Frame:
    """The top frame with the frames of its own process under their <iframe>: the
    snapshot holds every document of the process, `frames` their trees by frame id."""
    top = Frame(ax, snap, 0, (0.0, 0.0), session)
    strings, docs = snap["strings"], snap["documents"]
    holders = {0: top}
    for di, doc in enumerate(docs):
        holder = holders.get(di)
        if holder is None:
            continue
        nodes = doc["nodes"]
        boxes = {}
        for k, i in enumerate(doc["layout"]["nodeIndex"]):
            boxes.setdefault(i, doc["layout"]["bounds"][k])
        for i, child in zip(nodes["contentDocumentIndex"]["index"], nodes["contentDocumentIndex"]["value"]):
            inner = frames.get(strings[docs[child]["frameId"]])
            if inner is None:
                continue
            b = boxes.get(i, [0, 0, 0, 0])
            f = Frame(inner, snap, child, (holder.offset[0] + b[0], holder.offset[1] + b[1]), session)
            holder.children[nodes["backendNodeId"][i]] = f
            holders[child] = f
    return top


def render(top: Frame, view: View, ids: Ids,
           reaches: Callable[[Frame, int, float, float], bool], chrome: bool = True,
           scope: tuple[str, int] | None = None, whole: bool = False) -> Rendered:
    """The outline of what is in view. `reaches(frame, backend, x, y)`: the hit test
    at page x, y finds that node or something in it. `chrome` False leaves navigation,
    banners, footers and sidebars out. `scope` (session, backend id): that element's
    subtree alone, the whole of it. `whole`: the whole page, no hit tests."""
    out = Rendered([], {}, [])
    w = _Walker(view, ids, reaches, whole or scope is not None, out, chrome)
    if scope is None:
        items = w.frame(top)
    else:
        items = []
        for f in frames_in(top):
            if f.session != scope[0]:
                continue
            n = next((n for n in f.ax if n.get("backendDOMNodeId") == scope[1]), None)
            if n is not None:
                w.frame(f)
                items = w.node(n)
                break
    for item in items:
        w.emit(item, 0)
    return out


def frames_in(top: Frame):
    yield top
    for f in top.children.values():
        yield from frames_in(f)


def find(lines: list[str], want: str, context: int = 1) -> tuple[list[str], int]:
    """The lines of an outline that have `want` (words, any case, or /a regex/, /i for
    any case), each with `context` lines around it, gaps as "...", and how many have it."""
    m = re.fullmatch(r"/(.+)/([a-z]*)", want.strip(), re.S)
    if m:
        pattern = re.compile(m[1], re.I if "i" in m[2] else 0)
    else:
        pattern = re.compile(re.escape(" ".join(want.split())), re.I)
    hits = [i for i, ln in enumerate(lines) if pattern.search(ln)]
    keep = set()
    for i in hits:
        keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    out, last = [], -1
    for i in sorted(keep):
        if i != last + 1:
            out.append("...")
        out.append(lines[i])
        last = i
    return out, len(hits)


def page(top: Frame, view: View) -> str:
    """The whole page as Markdown, for reading."""
    w = _Walker(view, Ids(), lambda *a: True, True, Rendered([], {}, []))
    md = _Markdown()
    for item in w.frame(top):
        md.emit(item, 0)
    return md.text()


class _Walker:
    def __init__(self, view, ids, reaches, whole, out, chrome=True):
        self.view, self.ids, self.reaches, self.whole, self.out = view, ids, reaches, whole, out
        self.chrome = chrome
        self.seen = set(ids.by_node)

    # ---- one frame ----
    def frame(self, frame: Frame) -> list[Item]:
        self.f = frame
        self.ax = {n["nodeId"]: n for n in frame.ax}
        (self.box, self.parent, self.clickable, self.scrolls, self.selects,
         self.passwords) = geometry(frame)
        roots = [n for n in frame.ax if n["role"]["value"] == "RootWebArea"]
        return self.children(roots[0]) if roots else []

    def children(self, n: dict) -> list[Item]:
        items = []
        for cid in n.get("childIds", []):
            c = self.ax.get(cid)
            if c is not None:
                items.extend(self.node(c))
        return items

    def node(self, n: dict) -> list[Item]:
        role = n["role"]["value"]
        if role in SKIP or prop(n, "hidden") or (role in CHROME and not self.chrome):
            return []
        if n["ignored"]:
            return self.children(n)
        backend = n.get("backendDOMNodeId")
        if role == "StaticText":
            return self.text(n)
        if role in ("Iframe", "IframePresentational"):
            return self.iframe(n)
        b = self.where(backend)
        if role in INTERACTIVE or role in SELECTS or (
                backend in self.clickable and role in ("generic", "image") and self.small(b)):
            return self.interactive(n, "select" if backend in self.selects else role, b)
        if role in LEAVES:
            return self.leaf(n, role, b)
        if role in STRUCTURE:
            kids = self.children(n)
            if not kids:
                return []
            name = name_of(n)
            if kids[0].kind == "text" and kids[0].text == name:
                kids = kids[1:]                     # a legend, named twice
            label = WORDS.get(role, role) + (" " + quote(name) if name else "")
            return [Item("node", label + ":", kids, role, name)]
        return self.block(n, role)

    def where(self, backend) -> tuple | None:
        b = self.box.get(backend)
        while b is None and backend in self.parent:
            backend = self.parent[backend]
            b = self.box.get(backend)
        return b

    def small(self, b) -> bool:
        return b is not None and b[2] * b[3] < self.view.w * self.view.h / 2

    def in_view(self, b) -> bool:
        if b is None:
            return False
        x, y, w, h = b
        if self.whole:
            return w > 0 and h > 0
        v = self.view
        return w > 0 and h > 0 and x < v.x + v.w and x + w > v.x and y < v.y + v.h and y + h > v.y

    def reached(self, backend, b) -> bool:
        if self.whole or backend is None:
            return True
        x, y, w, h = b
        v = self.view
        cx = min(max(x + w / 2, v.x), v.x + v.w - 1)
        cy = min(max(y + h / 2, v.y), v.y + v.h - 1)
        return self.reaches(self.f, backend, cx, cy)

    def text(self, n: dict) -> list[Item]:
        backend = n.get("backendDOMNodeId")
        b = self.where(backend)
        if not self.in_view(b) or not self.reached(self.parent.get(backend, backend), b):
            return []
        t = name_of(n)
        return [Item("text", t)] if t else []

    def states(self, n: dict) -> list[str]:
        tags = []
        for s in STATES:
            v = prop(n, s)
            if v is True or v == "true":
                tags.append(s)
            elif v == "mixed":
                tags.append(f"{s}=mixed")
        if prop(n, "focused"):
            tags.append("active")
        return tags

    def interactive(self, n: dict, role: str, b) -> list[Item]:
        backend = n["backendDOMNodeId"]
        holder = role in ("generic", "image")      # a container with a click listener
        if not self.in_view(b) or not self.reached(backend, b):
            # a holder's own box says nothing of what is in it: a shadow host with a click
            # listener is 0x0 while its fixed dialog fills the view (reddit's login form)
            return self.children(n) if holder else []
        key = (self.f.session, backend)
        new = bool(self.seen) and key not in self.seen
        i = self.ids.get(*key)
        node = self.out.nodes[i] = Node(i, self.f.session, backend, role, b, self.f,
                                        password=backend in self.passwords)
        word = "password field" if node.password else WORDS.get(role, "clickable" if holder
                                                                else role)
        name = name_of(n)
        kids = self.children(n) if role in COMPOSITE or role in SELECTS or holder or role == "select" else []
        if holder and not name:
            if has_ref(kids):                       # the controls in it are what it clicks
                self.out.nodes.pop(i, None)
                return kids
            if len(kids) == 1 and kids[0].kind == "text":
                name, kids = cut(kids[0].text, 80), []
        value = value_of(n)
        if role in ("checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio") \
                and value in ("true", "false", "mixed"):
            value = ""
        tags = self.states(n)
        if new:
            tags.append("new")
        if prop(n, "hasPopup") and role != "select":
            tags.append("menu")
        line = word + (" " + quote(name) if name else "") + f" e{i}"
        if value:
            line += ": " + cut(value, NAME_MAX)
        if role == "select":                    # its options, shown or not (a closed list)
            node.options = self.options(n)
            shown = ", ".join(cut(o, 40) for o in node.options[:OPTIONS_MAX])
            more = len(node.options) - OPTIONS_MAX
            if shown:
                line += f" (options: {shown}" + (f", and {more} more)" if more > 0 else ")")
            kids = []
        elif role in COMPOSITE or role in SELECTS:
            kids = [k for k in kids if k.kind == "node"]
        elif not holder:
            kids = []
        if tags:
            line += " [" + ", ".join(tags) + "]"
        if not name and not kids and (role in ("link", "button") or holder):
            self.out.nodes.pop(i, None)
            return []
        if kids:
            line += ":"
        return [Item("node", line, kids, role, name, ref=i)]

    def options(self, n: dict) -> list[str]:
        out = []
        for c in n.get("childIds", []):
            k = self.ax.get(c)
            if k is None:
                continue
            out += [name_of(k)] if k["role"]["value"] == "option" else self.options(k)
        return out

    def leaf(self, n: dict, role: str, b) -> list[Item]:
        if not self.in_view(b) or not self.reached(n.get("backendDOMNodeId"), b):
            return []
        name = name_of(n)
        if role == "image" and (not name or b[2] < ICON_MAX or b[3] < ICON_MAX):
            return []
        if role == "separator":
            return []
        if role == "heading":
            level = int(prop(n, "level") or 0)
            kids = [k for k in self.children(n) if k.kind == "node"]
            line = "heading " + quote(name) + (f" [level={level}]" if level else "")
            return [Item("node", line + (":" if kids else ""), kids, role, name, level)]
        word = WORDS.get(role, role)
        return [Item("node", word + (" " + quote(name) if name else ""), [], role, name)]

    def block(self, n: dict, role: str) -> list[Item]:
        backend = n.get("backendDOMNodeId")
        kids = self.children(n)
        if not kids:
            return []
        b = self.where(backend)
        scroll = self.scrolls.get(backend)
        if scroll and not self.whole and self.in_view(b) and backend not in self.out.nodes:
            rect, client = scroll
            below, above = rect[3] - client[3] - rect[1], rect[1]
            if max(below, above) >= 20 and b[2] >= 40 and b[3] >= 40 and self.reached(backend, b):
                i = self.ids.get(self.f.session, backend)
                self.out.nodes[i] = Node(i, self.f.session, backend, "scrollarea", b, self.f)
                self.out.scrolls.append((i, round(below), round(above)))
                line = f"scrollarea e{i} [{round(below)} px below, {round(above)} px above]:"
                return [Item("node", line, kids, "scrollarea", ref=i)]
        if role not in BLOCKS:
            return kids                             # generic, group and the rest: what is in them
        return [Item("block", "", kids, role)]

    def iframe(self, n: dict) -> list[Item]:
        inner = self.f.children.get(n.get("backendDOMNodeId"))
        if inner is None or not self.in_view(self.where(n.get("backendDOMNodeId"))):
            return []
        saved = (self.f, self.ax, self.box, self.parent, self.clickable, self.scrolls, self.selects,
                 self.passwords)
        kids = self.frame(inner)
        (self.f, self.ax, self.box, self.parent, self.clickable, self.scrolls, self.selects,
         self.passwords) = saved
        if not kids:
            return []
        name = name_of(n)
        return [Item("node", "frame" + (" " + quote(name) if name else "") + ":", kids, "frame", name)]

    # ---- the outline ----
    def emit(self, item: Item, depth: int) -> None:
        pad = "  " * depth + "- "
        if item.kind == "text":
            if WORDY.search(item.text):
                self.out.lines.append(pad + "text: " + cut(item.text, RUN_MAX))
        elif item.kind == "node":
            self.out.lines.append(pad + item.text)
            for k in item.kids:
                self.emit(k, depth + 1)
        else:
            self.emit_block(item.kids, depth)

    def emit_block(self, kids: list[Item], depth: int) -> None:
        """A block's inline run is one text line, its links written inline as name[e14]
        when they sit between words; what is not inline goes under it. A run that only
        repeats the name of the field next to it (its label) is left out."""
        run: list[str] = []
        names = {k.name.strip(": *") for k in kids if k.kind == "node" and k.ref}

        def flush():
            text = " ".join(run)
            run.clear()
            if WORDY.search(text) and text.strip(": *") not in names:
                self.out.lines.append("  " * depth + "- text: " + cut(text, RUN_MAX))

        prose = any(k.kind == "text" and WORDY.search(k.text) for k in kids)
        for k in kids:
            if k.kind == "text":
                run.append(k.text)
            elif prose and k.kind == "node" and k.role == "link" and not k.kids and k.ref:
                run.append(f"{k.name}[e{k.ref}]")
            else:
                flush()
                self.emit(k, depth)
        flush()


class _Markdown:
    """The whole page for reading: headings, paragraphs, lists, quotes, code, tables;
    links as their text; no buttons, fields, navigation, banners or footers."""

    def __init__(self):
        self.lines: list[str] = []

    def text(self) -> str:
        out = "\n".join(self.lines)
        return re.sub(r"\n{3,}", "\n\n", out).strip()

    def run(self, kids: list[Item]) -> str:
        parts = []
        for k in kids:
            if k.kind == "text":
                parts.append(k.text)
            elif k.kind == "node" and k.role == "link":
                parts.append(k.name)
        return " ".join(" ".join(parts).split())

    def emit(self, item: Item, depth: int, prefix: str = "") -> None:
        if item.kind == "text":
            if WORDY.search(item.text):
                self.lines.append(prefix + item.text)
            return
        role = item.role
        if item.kind == "node":
            if role in CHROME:
                return
            if role == "heading":
                self.lines += ["", prefix + "#" * max(1, min(item.level or 2, 6)) + " " + item.name, ""]
                for k in item.kids:
                    if k.role != "link":
                        self.emit(k, depth, prefix)
            elif role == "link":
                self.lines.append(prefix + item.name)
            elif role in ("image", "video", "audio", "plugin", "Video", "Audio", "PluginObject"):
                self.lines.append(prefix + f"({WORDS.get(role, role)}: {item.name})" if item.name else "")
            elif role == "blockquote":
                for k in item.kids:
                    self.emit(k, depth, prefix + "> ")
            elif role == "row":
                cells = [self.run(k.kids) if k.kind == "block" else k.name for k in item.kids]
                self.lines.append(prefix + "| " + " | ".join(cells) + " |")
            elif role == "article":
                # a comment: its name says who and when; replies, the articles in it, quoted
                self.lines.append("")
                if item.name:
                    self.lines.append(prefix + "**" + item.name + "**")
                for k in item.kids:
                    self.emit(k, depth, prefix + ("> " if k.role == "article" or has_article(k) else ""))
                self.lines.append("")
            elif role in ("list", "DescriptionList", "table", "main", "region", "dialog",
                          "alertdialog", "form", "figure", "feed", "frame", "group"):
                if role in ("table", "list", "figure"):
                    self.lines.append("")
                for k in item.kids:
                    self.emit(k, depth + (role in ("list", "DescriptionList")), prefix)
                if role in ("table", "list", "figure"):
                    self.lines.append("")
            elif role in INTERACTIVE or role in SELECTS or role == "select":
                return                          # buttons and fields are not reading
            else:                               # a clickable holder and the rest: what is in it
                for k in item.kids:
                    self.emit(k, depth, prefix)
            return
        # a block
        inline = [k for k in item.kids if k.kind == "text" or (k.kind == "node" and k.role == "link")]
        rest = [k for k in item.kids if k not in inline]
        text = self.run(inline)
        if role == "listitem":
            self.lines.append(prefix + "  " * max(0, depth - 1) + "- " + text)
        elif role in ("code", "Pre") and ("\n" in text or len(text) > 60):
            self.lines += ["", prefix + "```", prefix + text, prefix + "```", ""]
        elif role in ("paragraph", "cell", "columnheader", "rowheader", "term", "definition",
                      "note", "caption", "Figcaption"):
            if WORDY.search(text):
                self.lines += ["", prefix + text, ""]
        elif WORDY.search(text):
            self.lines.append(prefix + text)
        for k in rest:
            self.emit(k, depth, prefix)
