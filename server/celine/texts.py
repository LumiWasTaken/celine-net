"""Every long text's History: her persona and talk, Bastet's persona and voice, a
mission's brief, a Routine's prompts, lyrics. Each is named
`<kind>:<id>[:<field>]` (web/src/lib/texts.ts TextOf). The place that saves a text calls
`record` in the same write, so History lists every version, newest first, PAGE at a time
(table text_versions).

`by` is stored as written (`user`, `her`, '' unknown) and shown as the name it stands
for now, so a rename carries."""
from __future__ import annotations

import re
import sqlite3

from .core import ids

# Every kind of long text, as `of` names it: the place that saves one calls `record`
# (tests/test_texts_wired.py checks each kind has such a place).
KINDS = ("her", "bastet", "routine", "mission", "lyrics")
PAGE = 50
CUT = 80                       # the most characters of a sentence a change line quotes
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]*")
_DASHES = re.compile(r"\s*(?:[\u2013\u2014]| \u00b7 )\s*")


def sentences(text: str) -> list[str]:
    return [s for s in (m.strip() for m in _SENTENCE.findall(text)) if s]


def _quote(s: str) -> str:
    s = _DASHES.sub(", ", s)
    return s if len(s) <= CUT else s[:CUT - 1].rstrip() + "\u2026"


def _n(items: list) -> str:
    return "" if len(items) == 1 else f" {len(items)} sentences"


def change(old: str | None, new: str) -> str:
    """One line of what changed from `old` (None: there was nothing) to `new`: the
    sentences added, removed or reworded, and the first of them, cut to 80 characters
    (as the approved History shows it: Added: "...")."""
    now = sentences(new)
    if old is None:
        return f'First version: "{_quote(now[0])}"' if now else "First version"
    if not now:
        return "Cleared the text"
    was = sentences(old)
    had, has = set(was), set(now)
    added = [s for s in now if s not in had]
    removed = [s for s in was if s not in has]
    if not added and not removed:
        return "Changed the order or spacing"
    if len(added) == len(removed):
        head = "Changed the wording of" if len(added) == 1 else f"Changed{_n(added)}:"
    elif added and removed:
        head = f"Added{_n(added) or ' 1 sentence'}, removed {len(removed)}:"
    elif added:
        head = f"Added{_n(added)}:"
    else:
        head = f"Removed{_n(removed)}:"
    return f'{head} "{_quote((added or removed)[0])}"'


def record(c: sqlite3.Connection, of: str, text: str, by: str) -> None:
    """Inside a write: `text` is the newest version of `of`, written by `by` ("user" or
    "her"). The same text as the newest version is not kept twice."""
    last = c.execute("SELECT text FROM text_versions WHERE of=? ORDER BY id DESC LIMIT 1",
                     (of,)).fetchone()
    if last is not None and last[0] == text:
        return
    c.execute("INSERT INTO text_versions(of, text, by, change, size, created_at) "
              "VALUES(?,?,?,?,?,?)",
              (of, text, by, change(last[0] if last else None, text), len(text), ids.now()))


def keep(c: sqlite3.Connection, of: str, was: str, text: str, by: str) -> None:
    """`record` for a text that existed before its first save (a shipped default): a text
    with no History yet first keeps how it was (by '', unknown), so the first change can
    be diffed and reverted."""
    if was != text and c.execute("SELECT 1 FROM text_versions WHERE of=? LIMIT 1",
                                 (of,)).fetchone() is None:
        record(c, of, was, "")
    record(c, of, text, by)


def who(cfg, by: str) -> str:
    """The name `by` stands for now: the user's or its own."""
    if by == "user":
        return cfg.user
    return cfg.name if by == "her" else by


def _view(cfg, r, full: bool = False) -> dict:
    v = {"id": r["id"], "at": r["created_at"], "by": who(cfg, r["by"]),
         "change": r["change"], "size": r["size"]}
    return {**v, "text": r["text"]} if full else v


def history(c: sqlite3.Connection, cfg, of: str, q: str = "",
            before: int | None = None) -> dict:
    """{versions, total, next}: `of`'s versions newest first, PAGE at a time below id
    `before`; `q` finds it in the change line or in who wrote it. `total` counts every
    match, `next` is the `before` of the next page (None on the last)."""
    where, args = "of=?", [of]
    if q := q.strip().lower():
        mine = [b for (b,) in c.execute("SELECT DISTINCT by FROM text_versions WHERE of=?",
                                        (of,)) if q in who(cfg, b).lower()]
        where += (" AND (instr(lower(change), ?) > 0 OR by IN (%s))"
                  % ",".join("?" * len(mine)))
        args += [q, *mine]
    total = c.execute(f"SELECT count(*) FROM text_versions WHERE {where}", args).fetchone()[0]
    if before is not None:
        where += " AND id < ?"
        args.append(before)
    rows = c.execute(f"SELECT id, of, by, change, size, created_at FROM text_versions "
                     f"WHERE {where} ORDER BY id DESC LIMIT ?", [*args, PAGE + 1]).fetchall()
    page = rows[:PAGE]
    return {"versions": [_view(cfg, r) for r in page], "total": total,
            "next": page[-1]["id"] if len(rows) > PAGE else None}


def version(c: sqlite3.Connection, cfg, vid: int) -> dict | None:
    r = c.execute("SELECT * FROM text_versions WHERE id=?", (vid,)).fetchone()
    return _view(cfg, r, full=True) if r else None


# ---- one version as stored, and what a text belongs to ------------------------------------------------

def raw(c: sqlite3.Connection, vid: int) -> sqlite3.Row | None:
    """Version `vid` as stored: of, text."""
    return c.execute("SELECT of, text FROM text_versions WHERE id=?", (vid,)).fetchone()


def about(c, cfg, of: str) -> list[str]:
    """What `of` is and what it belongs to, as lines for its prompt; an unknown kind or
    id: nothing (it has the text alone)."""
    kind, _, rest = of.partition(":")
    if kind == "routine":
        name, _, field = rest.partition(":")
        s = cfg.schedules.get(name)
        if s is None:
            return []
        lines = [f"This is the {field or 'text'} of the Routine {s.title or s.label or name}."]
        if field != "prompt" and s.prompt:
            lines.append(f"Its prompt:\n{s.prompt}")
        return lines
    if kind == "bastet":
        what = {"persona": "who she is", "talk": "how she replies to you on the Intercom",
                "note": "how she writes her notes to {{username}}"}.get(rest)
        return [cfg.substitute(f"This is Bastet's text of {what}: Bastet is the guard who "
                               "checks your steps.")] if what else []
    return []
