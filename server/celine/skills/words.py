"""A tool call's row in words, as the transcript shows it: what it did ("Sent you
funnytime.pdf", "Read 3 notes"), what it is doing ("Reading the note x"), what it
could not do ("Could not read the note x") or did not run ("Not run: ..."). Built here
from the call's name, arguments, display and result, sent as `words` beside `display`
on every tool event and snapshot row; the client renders it as it comes.

`words()` -> {running, settled, step, where, why}: `running` while the call is at work,
`settled` once it ended (for a call still at work: the words if it is cut off),
`step` the tool alone at work (a sub-agent's or a routine's step, no arguments),
`where` the places a search was kept to ("All messages"), or None, `why` a failed
call's reason (the first line of its result), or None.

One sentence per tool, grouped by the skill that owns it. An MCP server's tool takes its
sentence from its server's `[servers.<id>.words]` (mcp.toml or a plugin.toml), else it is
"Used <skill> <tool>".

A run (a stretch): two or more settled calls between two texts of a reply, shown as one
row in words, one part per `group` in the order first seen ("Read 2 notes, ran 4
commands"), with its failed count (`runs`). A call at work, gated, not run or apart keeps
its own row. The server decides the runs of a turn (`runs_of_turn`) and sends them with
every tool event and in the snapshot; the client only draws them."""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from celine.core.states import MISSION

Args = dict[str, Any]
BUSY = ("pending", "running", "detached")
_ELLIPSIS = "…"

SOURCE_LABEL = {"whatsapp": "WhatsApp", "discord": "Discord", "documents": "Documents",
                "telegram": "Telegram", "signal": "Signal", "email": "Email"}


# ---- small helpers, as the row has always said things -------------------------------

def _js(v: Any) -> str:
    """A value in text, as JavaScript's String() writes it."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dict, list)):
        return "[object Object]" if isinstance(v, dict) else ",".join(_js(x) for x in v)
    return str(v)


def _str(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _list(v: Any) -> list[str]:
    return [s for s in (_js(x) for x in v) if s] if isinstance(v, list) else []


def _num(v: Any) -> float | int | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def quote(s: str, max_: int = 60) -> str:
    return f'"{s[:max_ - 1] + _ELLIPSIS if len(s) > max_ else s}"'


def count(n: Any, one: str, many: str | None = None) -> str:
    v = n or 0
    return f"{v:,} {one if v == 1 else (many or one + 's')}"


def source_label(source: str | None) -> str:
    return SOURCE_LABEL.get(source, source) if source else ""


def _first_line(s: str, max_: int = 80) -> str:
    line = next((x.strip() for x in s.split("\n") if x.strip()), "")
    return line[:max_ - 1] + _ELLIPSIS if len(line) > max_ else line


def _lower(s: str) -> str:
    return s[:1].lower() + s[1:]


def _kind_word(kind: str) -> str:
    return "document" if kind == "markdown" else "page"


def _artifact(d: dict) -> dict | None:
    a = d.get("artifact")
    return a if isinstance(a, dict) and isinstance(a.get("id"), str) and \
        _num(a.get("n")) is not None else None


def _share(d: dict) -> dict | None:
    s = d.get("share")
    return s if isinstance(s, dict) and isinstance(s.get("token"), str) and \
        isinstance(s.get("title"), str) else None


def _batch(d: Any) -> dict | None:
    return d if isinstance(d, dict) and isinstance(d.get("id"), str) and \
        isinstance(d.get("items"), list) else None


def _scope_chats(d: dict) -> list[dict]:
    v = d.get("scope_chats")
    return [_dict(c) for c in v] if isinstance(v, list) else []


def _chat_name(c: dict) -> str:
    p = c.get("platform")
    return f"{_js(c.get('name') if c.get('name') is not None else c.get('ref'))}" \
           f"{f' ({source_label(p)})' if p else ''}"


def scope_words(d: dict, a: Args) -> str | None:
    """Where it looked: "all messages", "WhatsApp only", "Ann (WhatsApp) and Ben
    (Discord)"."""
    kind = d.get("scope_kind")
    if kind == "all":
        return "all messages"
    if kind == "platforms":
        kbs = [source_label(k) for k in _list(d.get("scope_kbs"))]
        return f"{' and '.join(kbs)} only" if kbs else None
    if kind == "chats":
        chats = _scope_chats(d)
        if not chats:
            return None
        if len(chats) > 2:
            return f"{len(chats)} chats"
        return " and ".join(_chat_name(c) for c in chats)
    scope = _list(a.get("scope"))           # still running: what it asked for
    if len(scope) == 1 and scope[0] == "all":
        return "all messages"
    if scope:
        return count(len(scope), "place")
    return None


def _url(url: str):
    u = urlsplit(url)
    if not (u.scheme and u.netloc):
        raise ValueError(url)
    return u


def _page_where(url: str) -> str:
    """A page by its host and path, no scheme or query: "github.com/KellerJordan/Muon"."""
    try:
        u = _url(url)
    except ValueError:
        return url
    path = u.path or "/"
    where = f"{u.netloc.rpartition('@')[2].lower()}{'' if path == '/' else path.rstrip('/')}"
    return where[:59] + _ELLIPSIS if len(where) > 60 else where


def _site(d: dict, a: Args) -> str:
    """The site a browser call was on: its host without www ("wikipedia.org")."""
    url = _str(d.get("url")) or _str(a.get("url"))
    if not url:
        return "the page"
    try:
        host = _url(url).netloc.rpartition("@")[2].lower()
    except ValueError:
        return "the page"
    return re.sub(r"^www\.", "", host) or "the page"


def _mails(d: dict, a: Args) -> str:
    n = d["messages"] if _num(d.get("messages")) is not None else len(_list(a.get("ids")))
    return count(n, "email") if n else "email"


# ---- the sentences, by skill ----------------------------------------------------------

# a tool that needs no arguments to say what it did
VERB = {
    "skills__load": "Loaded skills",
    "email__list_folders": "Listed the email folders",
    "appearance__set_portrait": "Set a new portrait", "me__read": "Read the persona",
    "me__edit": "Changed the Example lines", "me__whoami": "Checked the setup",
    "settings__propose": "Proposed a settings change",
    "image_generator__generate": "Made an image", "image_generator__read": "Checked the images",
    "image_generator__stop": "Stopped the images",
    "music_generator__generate": "Made music", "music_generator__read": "Checked the tracks",
    "music_generator__stop": "Stopped the music",
    "missions__update": "Updated the mission", "intercom__send": "Sent a message",
    "intercom__cancel": "Cancelled a message", "voice__send": "Sent a voice note",
    "missions__list": "Listed the missions", "missions__wait": "Set the mission to wait",
    "missions__read": "Read a mission",
    "missions__start": "Started a mission",
    "models__list": "Listed the models", "routines__list": "Listed the routines",
    "artifacts__list": "Listed the pages",
    "shell__list_shares": "Listed the shares", "shell__unshare": "Ended a share",
    "mind__think": "Wrote down a thought", "writer__draft": "Wrote a draft",
    "past_conversations__read_tool_call": "Read an earlier tool call",
}

Sentence = Callable[[Args, dict, str], str]


def _more(n: int) -> str:
    return f" and {count(n - 1, 'more phrase', 'more phrases')}" if n > 1 else ""


def _archive(d: dict, a: Args) -> str:
    where = scope_words(d, a)
    return "Messages" if not where or where == "all messages" else where


def _first_chat(d: dict) -> str | None:
    chats = _scope_chats(d)
    return _chat_name(chats[0]) if chats else None


def _search(a, d, r):
    words = _list(a.get("words"))
    phrases = words or _list(a.get("about"))
    return f"Searched {_archive(d, a)}" + \
        (f" for {quote(phrases[0])}{_more(len(phrases))}" if phrases else "")


def _count(a, d, r):
    words = _list(a.get("words"))
    return f"Counted in {_archive(d, a)}" + \
        (f" with {quote(words[0])}{_more(len(words))}" if words else "")


def _read_period(a, d, r):
    when = _str(a.get("when"))
    chat = _first_chat(d) or _str(a.get("chat")) or "a chat"
    return f"Read {chat}{f', {when}' if when else ''}"


def _scroll(a, d, r):
    chat = _first_chat(d) or "a chat"
    way = _str(a.get("direction"))
    return f"Scrolled up in {chat}" if way == "before" else \
        f"Scrolled down in {chat}" if way == "after" else f"Scrolled around a message in {chat}"


CHAT_ARCHIVE: dict[str, Sentence] = {
    "search": _search,
    "count": _count,
    # a reference is written [[ref]]: the row shows it as the message's chip
    "read_session": lambda a, d, r: "Read the chat session around " +
        (f"[[{_str(a.get('ref'))}]]" if _str(a.get("ref")) else "a message"),
    "read_period": _read_period,
    "scroll": _scroll,
    "get_messages": lambda a, d, r: "Read saved messages",
    "look_up": lambda a, d, r: f"Looked up {quote(_str(a.get('name')) or '')}",
    "mark_same_person": lambda a, d, r:
        f"Marked accounts as the same person: {quote(_str(a.get('name')) or '')}",
}


def _doc_read(a, d, r):
    # the server names the document; while it runs or when it failed, the arguments do
    title = _str(d.get("title"))
    if title:
        return f"Read {quote(title)}"
    doc = _str(a.get("document")) or _str(a.get("ref"))
    return "Read a document" if not doc else \
        f"Read document {doc}" if re.fullmatch(r"\d+", doc) else f"Read {quote(doc)}"


USER_DOCUMENTS: dict[str, Sentence] = {
    "search": lambda a, d, r: f"Searched your documents for {quote(_str(a.get('query')) or '')}",
    "read": _doc_read,
    "list": lambda a, d, r: "Listed your documents",
    "write": lambda a, d, r: f"Wrote the document "
        f"{_str(d.get('path')) or _str(a.get('path')) or ''}".strip(),
}


def _path(a: Args, key: str = "path") -> str:
    return _str(a.get(key)) or ""


NOTEBOOK: dict[str, Sentence] = {
    "write": lambda a, d, r: f"Wrote the note {_path(a)}".strip(),
    "read": lambda a, d, r: f"Read the note {_path(a)}".strip(),
    "edit": lambda a, d, r: f"Edited the note {_path(a)}".strip(),
    "search": lambda a, d, r: f"Searched the notebook for {quote(_str(a.get('query')) or '')}",
    "list": lambda a, d, r: "Listed the notebook",
    "delete": lambda a, d, r: f"Moved the note {_path(a)} to Trash",
    "move": lambda a, d, r: f"Moved the note {_path(a, 'src')} to {_path(a, 'dst')}",
}

PAST_CONVERSATIONS: dict[str, Sentence] = {
    "search": lambda a, d, r:
        f"Searched past chats for {quote(_str(a.get('query')) or '')}",
    "read": lambda a, d, r: f"Read the chat {quote(_str(d.get('title')))}"
        if _str(d.get("title")) else "Read an earlier chat",
    "list_day": lambda a, d, r: f"Listed the chats of {_str(a.get('date')) or 'a day'}",
    "list": lambda a, d, r: "Listed the newest chats",
}


def _agent_start(a, d, r):
    # its name as the row's words; what it may touch is in the opened call
    name = _str(d.get("name")) or _str(a.get("name")) or ""
    return f"{'Resumed' if d.get('resumed') is True else 'Started'} the sub-agent {quote(name)}"


SUB_AGENTS: dict[str, Sentence] = {
    "start": _agent_start,
    "stop": lambda a, d, r: f"Stopped sub-agent {quote(_str(a.get('name')) or '')}",
    "read": lambda a, d, r: f"{'Waited for' if _num(a.get('wait')) else 'Looked at'} "
                            f"sub-agent {quote(_str(a.get('name')) or '')}",
}

INTERCOM: dict[str, Sentence] = {
    "read": lambda a, d, r: f"Read thread #{_js(a['thread'])}"
        if _num(a.get("thread")) is not None else "Listed the jobs"
        if a.get("kind") == "job" else "Read the Intercom threads",
    "start_chat": lambda a, d, r: "Started a chat" +
        (f" {quote(_str(a.get('title')))}" if _str(a.get("title")) else ""),
}


def _art_write(a, d, r):
    made = _artifact(d)
    title = made.get("title") if made and made.get("title") is not None else _str(a.get("title"))
    kind = made.get("kind") if made and made.get("kind") is not None else \
        (_str(a.get("kind")) or "html")
    version = f", version {_js(made['n'])}" if made else ""
    return f"Wrote the {_kind_word(kind)}{f' {quote(_js(title))}' if title else ''}{version}"


def _art_read(a, d, r):
    title = (_artifact(d) or {}).get("title")
    return f"Read the page {quote(_js(title))}" if title else "Read a page"


ARTIFACTS: dict[str, Sentence] = {"write": _art_write, "read": _art_read}


def _presets(a, d, r):
    kind = _str(a.get("kind"))
    return "Listed the image models" if kind == "model" else \
        "Listed the LoRAs" if kind == "lora" else "Listed the image presets"


IMAGE_GENERATOR: dict[str, Sentence] = {
    "list_images": lambda a, d, r: "Listed the images in this chat",
    "list_presets": _presets,
}


def _terminal(a: Args, d: dict) -> str | None:
    return _str(d.get("terminal")) or _str(a.get("terminal"))


def _files(a: Args) -> str:
    ps = _list(a.get("path")) or [_str(a.get("path")) or ""]
    names = [p.split("/")[-1] for p in ps if p.split("/")[-1]]
    return names[0] if len(names) == 1 else f"{len(names)} files" if names else "a file"


def _share_words(a, d, r):
    title = (_share(d) or {}).get("title") or _str(a.get("title"))
    return f"Shared {quote(title)}" if title else "Shared from the shellbox"


def _in_terminal(a: Args, d: dict) -> str:
    term = _terminal(a, d)
    return f" in terminal {term}" if term else ""


def _shell_run(a, d, r):
    # what the agent said the command does, as it wrote it; else the command's first line, cut
    # (the whole command is in the call's arguments)
    if about := _str(a.get("about")):
        return about
    cmd = _first_line(_str(a.get("command")) or "")
    return f"Ran {cmd or 'a command'}{_in_terminal(a, d)}"


def _shell_type(a, d, r):
    text = _first_line(_str(d.get("text")) or _str(a.get("text")) or "")
    keys = _str(d.get("keys")) or " ".join(_list(a.get("keys")))
    said = f'Typed "{text}" and pressed {keys}' if text and keys else \
        f'Typed "{text}"' if text else f"Pressed {keys}" if keys else "Typed"
    return f"{said}{_in_terminal(a, d) or ' in a terminal'}"


def _shell_read(a, d, r):
    term = _terminal(a, d)
    if not term:
        return "Listed the terminals"
    return f"Read the log of terminal {term}" if _num(a.get("from")) is not None else \
        f"Read terminal {term}"


def _shell_wait(a, d, r):
    # what it waited for: the command at work there, its first line
    what = _first_line(_str(d.get("text")) or "")
    term = _terminal(a, d)
    return f"Waited for {what}{_in_terminal(a, d)}" if what else \
        f"Waited for terminal {term}" if term else "Waited for a terminal"


SHELL: dict[str, Sentence] = {
    "run": _shell_run,
    "type": _shell_type,
    "stop": lambda a, d, r: f"Stopped the program in terminal {_terminal(a, d)}"
        if _terminal(a, d) else "Stopped a program",
    "read": _shell_read,
    "wait": _shell_wait,
    "share": _share_words,
    "send": lambda a, d, r: f"Sent you {_files(a)}",
    "look": lambda a, d, r: f"Looked at {_files(a)}",
}

def _code_start(a, d, r):
    folder = _str(d.get("dir")) or _str(a.get("dir"))
    return f"{'Continued' if a.get('session_id') else 'Started'} a coding session" \
           f"{f' in {folder}' if folder else ''}"


CODE: dict[str, Sentence] = {
    "start": _code_start,
    "status": lambda a, d, r: f"Checked coding session {_path(a, 'session_id')}",
    "result": lambda a, d, r: f"Read the answer of coding session {_path(a, 'session_id')}",
    "abort": lambda a, d, r: f"Stopped coding session {_path(a, 'session_id')}",
    "list": lambda a, d, r: "Listed the coding sessions",
}


def _read_page(a, d, r):
    # the page's title, else its host; the url may be a search ref ("web:33")
    title = _str(d.get("title"))
    if title:
        return f"Read {quote(title)}"
    url = _str(d.get("url")) or _str(a.get("url"))
    if a.get("save") is True:
        return f"Saved a file from {_page_where(url) if url else 'the web'}"
    n = re.match(r"^\[*\s*web:(\d+)\s*\]*$", url) if url else None
    if n:
        return f"Read search result {n.group(1)}"
    return f"Read {_page_where(url) if url else 'a web page'}"


WEB: dict[str, Sentence] = {
    "search": lambda a, d, r: f"Searched the web for {quote(_str(a.get('query')) or '')}",
    "read_page": _read_page,
}


def _q_for(a: Args) -> str:
    q = _str(a.get("query"))
    return f" for {quote(q)}" if q else ""


def _chat_app(platform: str) -> dict[str, Sentence]:
    label = source_label(platform)
    return {
        "list_chats": lambda a, d, r: f"Listed {label} chats{_q_for(a)}",
        "read_messages": lambda a, d, r: f"Read messages live in {label}",
        "read_around_message": lambda a, d, r: f"Read the messages around a {label} message",
    }


DISCORD: dict[str, Sentence] = {
    **_chat_app("discord"),
    "search_messages": lambda a, d, r: f"Searched Discord for {quote(_str(a.get('query')) or '')}",
    "list_user_messages": lambda a, d, r: "Listed a user's Discord messages" +
        (f" on {_str(a.get('day'))}" if _str(a.get("day")) else ""),
    "get_user_profile": lambda a, d, r: "Got a Discord profile",
    "get_server_info": lambda a, d, r: "Got a Discord server",
    "list_members": lambda a, d, r: f"Listed Discord members{_q_for(a)}",
}

WHATSAPP: dict[str, Sentence] = {
    **_chat_app("whatsapp"),
    "request_older_messages": lambda a, d, r: "Asked your phone for older WhatsApp messages",
    "get_contact": lambda a, d, r: "Got a WhatsApp contact",
    "get_group": lambda a, d, r: "Got a WhatsApp group",
}

SKILLS: dict[str, Sentence] = {
    "read_guide": lambda a, d, r: f"Read the {_str(d.get('skill')) or _str(a.get('name')) or ''}"
                                  f" guide".replace("  ", " ", 1),
}


def _mail_list(a, d, r):
    folder = _str(d.get("folder")) or _str(a.get("folder")) or "INBOX"
    return "Looked at the inbox" if folder.upper() == "INBOX" else f"Looked at the emails in {folder}"


def _mail_search(a, d, r):
    sender = _str(a.get("from_")) or _str(a.get("from"))
    return f"Searched the emails{f' from {sender}' if sender else ''}{_q_for(a)}"


def _mail_read(a, d, r):
    # the sender's name from the headers the result starts with
    m = re.search(r"^From: (.+)$", r or "", re.M)
    sender = re.sub(r'^"|"$', "", re.sub(r"\s*<[^>]*>\s*$", "", m.group(1))).strip() if m else ""
    return f"Read the email from {sender}" if sender else "Read an email"


EMAIL: dict[str, Sentence] = {
    "list": _mail_list,
    "search": _mail_search,
    "read": _mail_read,
    "get_attachment": lambda a, d, r: f"Opened the attachment {_path(a, 'name')}".strip(),
    "mark": lambda a, d, r: f"Marked {_mails(d, a)} {_path(a, 'state')}".strip(),
    "move": lambda a, d, r: f"Moved {_mails(d, a)} to {_str(a.get('folder')) or 'a folder'}",
    "delete": lambda a, d, r: f"Moved {_mails(d, a)} to Trash",
}

VAULT: dict[str, Sentence] = {
    "get": lambda a, d, r: f"Got the vault entry {_str(a.get('name'))}"
        if _str(a.get("name")) else "Listed the vault",
    "set": lambda a, d, r: f"Made the vault entry {_path(a, 'name')}".strip(),
    "rotate": lambda a, d, r:
        f"Changed the password of {_str(a.get('name')) or 'a vault entry'}",
}


def _act(a: Args, where: str) -> str:
    """What browser__act did: its first steps in words ("Typed "Hedy Lamarr", then
    pressed Enter"), the rest counted."""
    steps = [_dict(s) for s in a["steps"]] if isinstance(a.get("steps"), list) else \
        [a] if a.get("do") else []
    said = []
    for s in steps:
        text = _str(s.get("text"))
        do = s.get("do")
        if do == "click":
            said.append(f"Clicked on {where}")
        elif do == "locate":
            said.append(f"Clicked on {where}" if (_str(s.get("then")) or "click") == "click"
                        else f"Used {where}")
        elif do == "type":
            said.append(f"Typed {quote(text)}" if text else "Typed")
        elif do == "key":
            said.append(f"Pressed {text or _str(s.get('key')) or 'a key'}")
        elif do == "open":
            said.append(f"Opened {_site({}, {'url': s.get('url')})}")
        elif do == "scroll":
            said.append("Scrolled")
        elif do == "select":
            said.append(f"Chose {quote(text)}" if text else "Chose an option")
        elif do == "wait":
            said.append("Waited")
        elif do in ("accept", "dismiss"):
            said.append("Answered a dialog")
        else:
            said.append(f"Used {where}")
    if not said:
        return f"Used {where}"
    shown = said[:2] if len(said) > 3 else said
    rest = len(said) - len(shown)
    return ", then ".join([shown[0], *map(_lower, shown[1:])]) + \
        (f", and {count(rest, 'more step', 'more steps')}" if rest else "")


def _browser_scroll(a, d, r):
    way = _str(a.get("direction"))
    if _str(a.get("to")):
        return "Scrolled to a part of the page"
    return f"Scrolled to the {way}" if way in ("top", "bottom") else f"Scrolled {way or 'down'}"


def _browser_look(a, d, r):
    what = _str(a.get("what")) or "view"
    find, ask = _str(a.get("find")), _str(a.get("question"))
    site = _site(d, a)
    if what == "screen":
        return f"Asked about the screen {quote(ask)}" if ask else f"Looked at the screen of {site}"
    if find:
        return f"Looked for {quote(find)} on {site}"
    if what == "page":
        return f"Read {site}"
    if what in ("html", "source"):
        return f"Read the {'HTML' if what == 'html' else 'source'} of {site}"
    return f"Looked at {site}"


BROWSER: dict[str, Sentence] = {
    "scroll": _browser_scroll,
    "close": lambda a, d, r: f"Closed the tab {_path(a, 'tab')}".strip(),
    "open": lambda a, d, r: f"Opened {_site(d, a)}",
    "look": _browser_look,
    "act": lambda a, d, r: _act(a, _site(d, a)),
}


def _plan_step(a, d, r):
    n, state = _num(a.get("step")), _str(a.get("state"))
    as_ = {"todo": "as to do", "doing": "as in progress", "done": "as done"}.get(state or "", state or "")
    text = f"Marked step {'' if n is None else _js(n)} {as_}"
    return re.sub(r" +", " ", text).strip()


PLAN: dict[str, Sentence] = {
    "set": lambda a, d, r: f"Set a plan of {count(len(a['steps']), 'step')}"
        if isinstance(a.get("steps"), list) and a["steps"] else "Set a plan",
    "step": _plan_step,
}

MIND: dict[str, Sentence] = {
    "feel": lambda a, d, r: "Changed the mood" +
        (f" to {_str(a.get('emotion'))}" if _str(a.get("emotion")) else ""),
    "set_status": lambda a, d, r: "Set the status" +
        (f" to {_str(a.get('text'))}" if _str(a.get("text")) else ""),
}

def _model_name(a: Args, d: dict) -> str:
    """A model as Settings labels it (the tool's display), else the model part of its
    ref; never the provider's address."""
    if label := _str(d.get("label")):
        return label
    ref = _str(a.get("model")) or ""
    return ref.partition("/")[2] or ref or "a model"


MODELS: dict[str, Sentence] = {
    "ask": lambda a, d, r: f"Asked {_model_name(a, d)}",
}

SETTINGS: dict[str, Sentence] = {
    "read": lambda a, d, r: f"Read the {label} settings"
    if (label := _str(d.get("label"))) else
    "Read the settings" if _str(a.get("target")) else "Listed the settings",
}

ROUTINES: dict[str, Sentence] = {
    "read": lambda a, d, r: f"Read the routine {_path(a, 'name')}".strip(),
    "run": lambda a, d, r: f"Ran the routine {_path(a, 'name')}".strip(),
    "write": lambda a, d, r: f"Wrote the routine {_path(a, 'name')}".strip(),
}

BUGREPORT: dict[str, Sentence] = {
    "file": lambda a, d, r: "Filed a bug" +
        (f": {_str(a.get('title'))}" if _str(a.get("title")) else ""),
    "list": lambda a, d, r: "Read the bug reports",
}

SENTENCES: dict[str, dict[str, Sentence]] = {
    "chat_archive": CHAT_ARCHIVE, "user_documents": USER_DOCUMENTS, "notebook": NOTEBOOK,
    "past_conversations": PAST_CONVERSATIONS, "sub_agents": SUB_AGENTS,
    "intercom": INTERCOM, "artifacts": ARTIFACTS, "image_generator": IMAGE_GENERATOR,
    "shell": SHELL, "code": CODE, "web": WEB, "discord": DISCORD, "whatsapp": WHATSAPP,
    "skills": SKILLS, "email": EMAIL, "vault": VAULT, "browser": BROWSER, "plan": PLAN,
    "mind": MIND, "models": MODELS, "routines": ROUTINES, "bugreport": BUGREPORT,
    "settings": SETTINGS,
}


# every MCP server's own sentences and groups, by server and tool: Config.tool_words
# (mcp.row_words), handed to each function here that says a call as `own`
Own = dict[str, dict[str, dict[str, str]]]


def _own(own: Own | None, kind: str, skill: str, tool: str) -> str | None:
    return (own or {}).get(skill, {}).get(kind, {}).get(tool)


# a hole in such a sentence: {argument} or {argument|what it says without one}; quotes
# around a hole leave with it when the argument is missing
_HOLE = re.compile(r'("?)\{(\w+)(?:\|([^}]*))?\}("?)')


def fill(template: str, a: Args) -> str:
    """`template` with each hole the call's argument: its first line, cut to 60."""
    def one(m: re.Match) -> str:
        v = a.get(m.group(2))
        text = _first_line(", ".join(_list(v)) if isinstance(v, list) else
                           "" if v is None or isinstance(v, dict) else _js(v), 60)
        # a hole that says nothing leaves a mark: the spaces around it close up, and only those
        return f"{m.group(1)}{text}{m.group(4)}" if text else m.group(3) or "\0"
    return re.sub(r" *\0 *", " ", _HOLE.sub(one, template)).strip()


def sentence(name: str, a: Args, d: dict, result: str = "", own: Own | None = None) -> str:
    """What it did (and where), in one plain sentence, past tense."""
    skill, _, tool = name.partition("__")
    fn = SENTENCES.get(skill, {}).get(tool)
    if fn is not None:
        return fn(a, d, result)
    if name in VERB:
        return VERB[name]
    template = _own(own, "words", skill, tool)
    if template:
        return fill(template, a)
    # a tool without its own sentence: a tool from an MCP server
    parts = name.split("__")
    skill, tool = parts[0], parts[1] if len(parts) > 1 else ""
    what = _str(a.get("query")) or _str(a.get("q"))
    tool = f" {tool.replace('_', ' ')}" if tool else ""
    return f"Used {skill.replace('_', ' ')}{tool}{f' for {quote(what)}' if what else ''}"


# ---- the same sentence at work and after a failure ------------------------------------

# "Read the note x" at work is "Reading the note x", failed "Could not read the note x"
VERBS = {
    "Searched": ("Searching", "search"), "Counted": ("Counting", "count"),
    "Read": ("Reading", "read"), "Scrolled": ("Scrolling", "scroll"),
    "Got": ("Getting", "get"), "Looked": ("Looking", "look"), "Marked": ("Marking", "mark"),
    "Listed": ("Listing", "list"), "Wrote": ("Writing", "write"), "Edited": ("Editing", "edit"),
    "Deleted": ("Deleting", "delete"), "Moved": ("Moving", "move"),
    "Started": ("Starting", "start"), "Proposed": ("Proposing", "propose"), "Ran": ("Running", "run"), "Checked": ("Checking", "check"),
    "Asked": ("Asking", "ask"), "Stopped": ("Stopping", "stop"), "Sent": ("Sending", "send"),
    "Used": ("Using", "use"), "Loaded": ("Loading", "load"), "Opened": ("Opening", "open"),
    "Closed": ("Closing", "close"), "Continued": ("Continuing", "continue"),
    "Changed": ("Changing", "change"), "Updated": ("Updating", "update"),
    "Cancelled": ("Cancelling", "cancel"), "Listened": ("Listening", "listen"),
    "Made": ("Making", "make"), "Typed": ("Typing", "type"), "Resumed": ("Resuming", "resume"),
    "Waited": ("Waiting", "wait"), "Set": ("Setting", "set"), "Clicked": ("Clicking", "click"),
    "Filed": ("Filing", "file"), "Pressed": ("Pressing", "press"),
    "Chose": ("Choosing", "choose"), "Answered": ("Answering", "answer"),
    "Saved": ("Saving", "save"), "Took": ("Taking", "take"), "Filled": ("Filling", "fill"),
    "Went": ("Going", "go"), "Pointed": ("Pointing", "point"), "Dragged": ("Dragging", "drag"),
    "Uploaded": ("Uploading", "upload"), "Resized": ("Resizing", "resize"),
    "Recorded": ("Recording", "record"),
    "Pasted": ("Pasting", "paste"), "Dropped": ("Dropping", "drop"),
    "Selected": ("Selecting", "select"), "Switched": ("Switching", "switch"),
    "Hovered": ("Hovering", "hover"), "Created": ("Creating", "create"),
}


def ongoing(done: str) -> str:
    verb, sep, rest = done.partition(" ")
    return f"{VERBS[verb][0] if verb in VERBS else verb}{sep}{rest}"


def could_not(done: str) -> str:
    verb, sep, rest = done.partition(" ")
    if verb in VERBS:
        return f"Could not {VERBS[verb][1]}{sep}{rest}"
    return f"Could not do this: {_lower(done)}"


def _first_sentence(s: str) -> str:
    return re.sub(r"\.$", "", re.split(r"(?<=[.!?])\s", s)[0].strip())


def not_run(state: str, d: dict) -> bool:
    """A call that did not run: the turn's time was up, or gated (its skill down, not
    loaded or off)."""
    return state == "gated" or (state == "error" and d.get("time_up") is True)


def _state_words(said: str, state: str, d: dict, result: str) -> str:
    if state in BUSY:
        return ongoing(said)
    if not_run(state, d):
        why = re.sub(r"^Not run: ", "", _first_sentence(result))
        down = re.match(r"^Down: (.+)$", why)
        if down:
            return f"{could_not(said)}: {down.group(1)}"      # its casing: names stay names
        return f"Not run: {why}" if why else "Not run"
    return could_not(said) if state == "error" else said


MEDIA = ("image_generator", "music_generator")
WHY_CHARS = 200          # the reason under a failed row; the whole result is in the call


def row(name: str, a: Args, d: dict, result: str, state: str, own: Own | None = None) -> str:
    """The row's words in `state`: what it did, is doing, could not do or did not run."""
    said = sentence(name, a, d, result, own)
    skill, _, tool = name.partition("__")
    tool = tool.split("__")[0]
    if name == "shell__run" and (about := _str(a.get("about"))):
        # the agent's words in every state (the icon says it), but a call not run says why
        return _state_words(about, state, d, result) if not_run(state, d) else about
    if name == "intercom__cancel":
        said = f"Cancelled message #{'' if a.get('id') is None else _js(a['id'])}"
        return _state_words(said, state, d, result)
    if name == "missions__update":
        status = _str(d.get("status")) or _str(a.get("status"))
        note = _str(d.get("note")) or _str(a.get("note")) or ""
        # the model's enum ("Needs you") or the stored status, said as the Missions page says it
        status = {"Active": "active", "Needs you": "needs_you", "Done": "done"}.get(status, status)
        word = MISSION[status][0].lower() if status in MISSION else "update"
        return f"Mission {word}{f': {note}' if note else ''}"
    if skill in MEDIA and (_batch(d) or tool in ("generate", "read", "stop")):
        items = a.get("items")
        first = _dict(items[0]) if isinstance(items, list) and items else {}
        made = tool not in ("read", "stop")
        label = ("Music" if skill == "music_generator" else "Images") if made else \
            ("Checked the " if tool == "read" else "Stopped the ") + \
            ("tracks" if skill == "music_generator" else "images")
        prompt = (_str(first.get("prompt")) or _str(first.get("style"))) if made else None
        return " ".join(x for x in (label, prompt) if x)
    return _state_words(said, state, d, result)


# the words of several calls of one tool in a stretch of work, {n} their count; the
# browser's tools are one group (GROUPS_OF)
GROUPS = {
    "notebook__read": "Read {n} notes", "notebook__write": "Wrote {n} notes",
    "notebook__edit": "Edited {n} notes", "notebook__search": "Searched the notebook {n} times",
    "settings__read": "Read the settings {n} times", "routines__read": "Read {n} routines",
    "routines__run": "Ran {n} routines", "web__search": "Searched the web {n} times",
    "web__read_page": "Read {n} web pages", "models__ask": "Asked models {n} times",
    "user_documents__read": "Read {n} documents",
    "user_documents__search": "Searched the documents {n} times",
    "chat_archive__search": "Searched the messages {n} times",
    "chat_archive__read_period": "Read {n} chats",
    "past_conversations__read": "Read {n} past chats",
    "past_conversations__search": "Searched past chats {n} times",
    "email__read": "Read {n} emails", "email__search": "Searched the emails {n} times",
}
GROUPS_OF = {"browser": "Used the browser {n} times"}


def group(name: str, a: Args, own: Own | None = None) -> str | None:
    """The words of several calls like this one in a stretch, {n} their count; None: it
    makes a part of its own. From the arguments only, so a call is in the same group when
    it starts and once it ran."""
    if name == "shell__run":
        term = _str(a.get("terminal"))
        return f"Ran {{n}} commands in terminal {term}" if term else "Ran {n} commands"
    skill, _, tool = name.partition("__")
    return GROUPS.get(name) or GROUPS_OF.get(skill) or _own(own, "groups", skill, tool)


# ---- runs: a stretch of work as one line ----------------------------------------------

PLUMBING = ("skills__load", "skills__read_guide")   # never shown beside other calls
# calls the user acts on or looks at as themselves: they keep their own row or block
APART = ("sub_agents__start", "intercom__send")


def failed(state: str, exit_: Any) -> bool:
    """A call that failed: an error, or a command that exited non-zero."""
    return state == "error" or bool(_num(exit_))


def _alone(state: str, time_up: Any) -> bool:
    """A call that is never folded into a run: one at work (its line, a job's Stop), gated
    or not run. You see it, or act on it, as itself; it joins its run once it ran."""
    return state in BUSY or state == "gated" or (state == "error" and bool(time_up))


def _apart(call: dict) -> bool:
    """A call that keeps its own row or block: a sub-agent (Open, Stop), a message it sent
    (Cancel), a batch of pictures, a page, a share, files it sent."""
    if call["name"] in APART:
        return True
    d = _dict(_args(call.get("display")))
    return isinstance(d.get("items"), list) or bool(d.get("artifact")) or \
        bool(d.get("share")) or isinstance(d.get("blocks"), list)


def _stretch_words(calls: list[dict], own: Own | None) -> str:
    """A stretch in words: one part per group, in the order first seen ("Read 2 notes,
    read the settings 4 times, ran 4 commands"); a group of one, or a call of no group,
    says its own sentence."""
    parts: dict[str, list[dict]] = {}
    for c in calls:
        a = _args(c["arguments"])
        key = group(c["name"], a if c["name"] == "shell__run" else {}, own) or f"\0{c['id']}"
        parts.setdefault(key, []).append(c)
    out: list[tuple[str, bool]] = []      # (words, as_written: its own words keep their casing)
    for key, members in parts.items():
        if len(members) > 1:
            out.append((key.replace("{n}", f"{len(members):,}"), False))
        else:
            c = members[0]
            a = _args(c["arguments"])
            out.append((sentence(c["name"], a, _dict(_args(c.get("display"))), "", own),
                        c["name"] == "shell__run" and bool(_str(a.get("about")))))
    return ", ".join([out[0][0], *(p if as_written else _lower(p) for p, as_written in out[1:])])


def runs(blocks: list[list[dict]], own: Own | None = None) -> list[dict]:
    """The runs among `blocks`, each the calls between two texts of a reply, in order
    (each {id, name, arguments, state, exit, time_up, display}): every settled call of a
    block that is not plumbing, at work or apart is one stretch, in words, when there are
    two or more. A call at work, gated, not run or apart keeps its own row and cuts
    nothing. -> [{ids, words, failed}]"""
    out: list[dict] = []
    for block in blocks:
        members = [c for c in block if c["name"] not in PLUMBING
                   and not _alone(c["state"], c.get("time_up")) and not _apart(c)]
        if len(members) < 2:
            continue
        out.append({"ids": [c["id"] for c in members],
                    "words": _stretch_words(members, own),
                    "failed": sum(failed(c["state"], c.get("exit")) for c in members)})
    return out


def _blocks(c, turn_id: str) -> list[list[dict]]:
    """A turn's calls in order, cut where a step of its reply has text."""
    blocks: list[list[dict]] = []
    last = None
    for r in c.execute(
            "SELECT m.id AS mid, m.content != '' AND trim(m.content) != '' AS said, "
            "tc.id, tc.name, tc.arguments, tc.state, tc.display, "
            "json_extract(tc.display, '$.exit') AS exit, "
            "json_extract(tc.display, '$.time_up') AS time_up "
            "FROM messages m LEFT JOIN tool_calls tc ON tc.message_id = m.id "
            "WHERE m.turn_id=? AND m.role='assistant' "
            "ORDER BY m.created_at, m.rowid, tc.rowid", (turn_id,)):
        if r["mid"] != last and (not blocks or r["said"]):
            blocks.append([])
        last = r["mid"]
        if r["id"] is not None:
            blocks[-1].append(dict(r))
    return blocks


def runs_of_turn(c, turn_id: str, own: Own | None = None) -> list[dict]:
    """runs() of a turn's calls: a reply's text between two calls ends a run."""
    return runs(_blocks(c, turn_id), own)


def runs_after(c, call_ids: list[str], own: Own | None = None, *,
               reset: bool = False) -> dict | None:
    """For the event of calls that just settled (or, `reset`, were set pending again): their
    turn's runs, {turn, list}; None outside a turn, or when the runs are the same as with
    those calls still at work, and the event goes without (the client keeps the last)."""
    r = c.execute("SELECT m.turn_id FROM tool_calls tc JOIN messages m ON m.id = tc.message_id "
                  "WHERE tc.id=?", (call_ids[0],)).fetchone() if call_ids else None
    if not r or not r["turn_id"]:
        return None
    blocks = _blocks(c, r["turn_id"])
    now_ = runs(blocks, own)
    if not reset:
        ids = set(call_ids)
        if now_ == runs([[{**x, "state": "pending"} if x["id"] in ids else x for x in b]
                         for b in blocks], own):
            return None
    return {"turn": r["turn_id"], "list": now_}


def _args(arguments: Any) -> Args:
    if isinstance(arguments, dict):
        return arguments
    try:
        v = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def step(name: str, own: Own | None = None) -> str:
    """The tool alone at work: "Reading the note"."""
    return ongoing(VERB.get(name) or sentence(name, {}, {}, "", own))


def words(name: str, arguments: Any, state: str, display: Any = None,
          result: str | None = None, own: Own | None = None) -> dict:
    """The row's words: {running, settled, step, where, why} (the module's docstring).
    `why`: a failed call's reason, the first line of its result as it came (a server's
    own words, errors.said), else None."""
    a, d, r = _args(arguments), _dict(display), result or ""
    where = scope_words(d, a)
    failed = state == "error" and not not_run(state, d)
    return {"running": row(name, a, d, r, "running", own),
            "settled": row(name, a, d, r, "interrupted" if state in BUSY else state, own),
            "step": step(name, own),
            "where": where[:1].upper() + where[1:] if where else None,
            "why": _first_line(r, WHY_CHARS) or None if failed else None}


def of_call(c, call_id: str, state: str, display: Any = None,
            result: str | None = None, own: Own | None = None) -> dict:
    """words() of a stored call, by its id (its name and arguments from its row)."""
    r = c.execute("SELECT name, arguments FROM tool_calls WHERE id=?", (call_id,)).fetchone()
    return words(r["name"], r["arguments"], state, display, result, own) if r else \
        words("", "{}", state, display, result, own)


_FIELD = {k: re.compile(rf'"{k}"\s*:\s*"((?:[^"\\]|\\.)*)"')
          for k in ("path", "name", "title", "query", "url", "terminal", "command", "to",
                    "folder", "text", "file_path", "input", "element", "key", "about")}


def forming(name: str, partial: str, own: Own | None = None) -> str:
    """A call it is still writing, from the arguments so far: what it is about to do
    ("Reading the note ml/muon.md")."""
    a = {}
    for k, rx in _FIELD.items():
        m = rx.search(partial)
        if m:
            a[k] = re.sub(r"\\(.)", r"\1", m.group(1))
    if name == "shell__run" and a.get("about"):
        return a["about"]               # the agent's words as it wrote them
    return ongoing(sentence(name, a, {}, "", own))


def prompt_of(arguments: str | None, tool_name: str = "") -> str | None:
    """What a call was asked, when its arguments say so in one field: a command's
    `command`, a sub-agent's `name` or `brief`, the `prompt` or `style` of a batch's
    first item. A shell call: the first line of what it typed (a pasted heredoc
    stays one line in the jobs list), else its keys, else its terminal. None for
    every other tool."""
    try:
        a = json.loads(arguments or "")
    except ValueError:
        return None
    if not isinstance(a, dict):
        return None
    if tool_name.startswith("shell__"):
        text = next((a[k] for k in ("command", "text") if isinstance(a.get(k), str)), "")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines:
            return lines[0]
        if isinstance(a.get("keys"), str) and a["keys"].strip():
            return a["keys"].strip()
        return str(a.get("terminal") or "main")
    items = a.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        a = items[0]
    for key in ("command", "name", "brief", "prompt", "style"):
        if isinstance(a.get(key), str) and a[key].strip():
            return a[key].strip()
    return None
