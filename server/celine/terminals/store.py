"""Terminal rows, the user's commands in them, and the commands union: plain functions
over the store (docs/ARCHITECTURE.md, terminals)."""
from __future__ import annotations

import gzip
import json
import re
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from celine.core.ids import new_id, now

NAME_MAX = 16          # the daemon's rule for a terminal name
# what the daemon said when a call returned while its program still ran: a snapshot,
# settled from what came after (_settled)
SNAPSHOT = ("running", "waiting", "fullscreen")
OUTCOME = ("state", "exit", "seconds", "running_for", "quiet_for", "program", "paused")

PLAIN_ID = re.compile(r"[a-z0-9]+")     # a daemon id, safe as a file name



def slug(name: str) -> str:
    """A terminal name from a mission's or a sub-agent's name: lowercase, anything else
    becomes one dash, 16 characters at most, first and last a letter or digit. May be
    empty ("!!")."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:NAME_MAX].rstrip("-")


def free_name(base: str, held: set[str]) -> str:
    """base, base-2, base-3: the first not held. A suffix never pushes the name past the
    daemon's 16 characters."""
    n = 1
    while True:
        tail = "" if n == 1 else f"-{n}"
        name = base[:NAME_MAX - len(tail)].rstrip("-") + tail
        if name not in held:
            return name
        n += 1


SHELL_CALLS = "tc.name LIKE 'shell\\_\\_%' ESCAPE '\\'"

# The agent's commands: one row per shell tool call (a run's text is its command, a stop's keys C-c). The terminal id comes from the result's
# display; a call still at work has no result yet, so it is the open terminal of that
# name. A shell__read without a terminal lists the terminals: terminal {null, ""}.
_HERS = f"""
SELECT tc.id AS id,
       CASE WHEN tc.name = 'shell__read' THEN 'read' ELSE 'command' END AS kind,
       coalesce(tc.started_at, a.created_at) AS at,
       coalesce(CASE WHEN json_valid(tc.display)
                     THEN json_extract(tc.display, '$.terminal_id') END,
                (SELECT t.id FROM terminals t
                 WHERE tc.state IN ('pending', 'running', 'detached')
                   AND t.ended_at IS NULL AND t.name = CASE WHEN json_valid(tc.arguments)
                     THEN json_extract(tc.arguments, '$.terminal') END)) AS terminal_id,
       CASE WHEN json_valid(tc.arguments)
                 AND json_extract(tc.arguments, '$.terminal') IS NOT NULL
                 AND json_extract(tc.arguments, '$.terminal') <> ''
            THEN json_extract(tc.arguments, '$.terminal')
            WHEN tc.name = 'shell__read' THEN ''
            ELSE 'main' END AS terminal_name,
       CASE WHEN mi.conversation_id IS NOT NULL THEN 'mission'
            WHEN c.agent_of IS NOT NULL THEN 'sub_agent'
            ELSE 'conversation' END AS from_kind,
       c.id AS conversation_id, c.agent_of AS agent_of, c.title AS title,
       CASE WHEN json_valid(tc.arguments)
            THEN coalesce(json_extract(tc.arguments, '$.command'),
                          json_extract(tc.arguments, '$.text')) END AS text,
       CASE WHEN tc.name = 'shell__stop' THEN 'C-c'
            WHEN json_valid(tc.arguments) THEN json_extract(tc.arguments, '$.keys') END AS keys,
       tc.display AS display, tc.state AS state, r.content AS result, NULL AS note,
       tc.job_id AS job_id
FROM tool_calls tc
JOIN messages a ON a.id = tc.message_id
JOIN conversations c ON c.id = a.conversation_id
LEFT JOIN missions mi ON mi.conversation_id = c.id
LEFT JOIN messages r ON r.id = tc.result_message_id
WHERE {SHELL_CALLS}
"""

# The user's commands: what they typed (line) and the buttons on its terminals (control).
_YOURS = """
SELECT k.id AS id, k.kind AS kind, k.created_at AS at, k.terminal_id AS terminal_id,
       t.name AS terminal_name, 'you' AS from_kind, NULL AS conversation_id,
       NULL AS agent_of, NULL AS title, k.text AS text, NULL AS keys, NULL AS display,
       NULL AS state, k.result AS result, k.note AS note, NULL AS job_id
FROM terminal_commands k
JOIN terminals t ON t.id = k.terminal_id
"""


async def upsert(store, id: str, name: str, owner: str, opened_at: str,
                 opened_by: str | None = None) -> None:
    """A terminal the daemon reports; a known id changes nothing but `opened_by`, the
    conversation whose call opened it, when the row has none yet (the sync can list a
    terminal before the call that opened it answers)."""
    await store.write(lambda c: c.execute(
        "INSERT INTO terminals(id, name, owner, opened_at, opened_by) VALUES(?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET opened_by = coalesce(opened_by, excluded.opened_by)",
        (id, name, owner, opened_at, opened_by)))


async def get(store, id: str) -> dict | None:
    return await store.read(lambda c: _one(c, "id=?", (id,)))


async def open_by_name(store, name: str) -> dict | None:
    return await store.read(lambda c: _one(c, "name=? AND ended_at IS NULL", (name,)))


async def list_terminals(store, trashed: bool = False) -> list[dict]:
    """Open terminals first, newest first; ended ones after. With `trashed` the
    trashed ones instead."""
    return await store.read(lambda c: [_view(r) for r in c.execute(
        "SELECT * FROM terminals WHERE (trashed_at IS NOT NULL) = ? "
        "ORDER BY ended_at IS NOT NULL, opened_at DESC, rowid DESC", (trashed,))])


async def opened_by(store, cid: str) -> list[dict]:
    """The open terminals conversation `cid` opened."""
    return await store.read(lambda c: [_view(r) for r in c.execute(
        "SELECT * FROM terminals WHERE opened_by=? AND ended_at IS NULL", (cid,))])


async def opener(store, id: str) -> dict | None:
    """The conversation that opened this terminal: {id, title, agent_of, kind (mission,
    sub_agent or conversation)}; None for the user's own."""
    def fn(c: sqlite3.Connection) -> dict | None:
        r = c.execute(
            "SELECT c.id, c.title, c.agent_of, CASE WHEN mi.conversation_id IS NOT NULL "
            "THEN 'mission' WHEN c.agent_of IS NOT NULL THEN 'sub_agent' "
            "ELSE 'conversation' END AS kind FROM terminals t "
            "JOIN conversations c ON c.id = t.opened_by "
            "LEFT JOIN missions mi ON mi.conversation_id = c.id WHERE t.id=?",
            (id,)).fetchone()
        return dict(r) if r else None

    return await store.read(fn)


async def last_ended_by(store, name: str) -> str | None:
    """How the terminal of this name that ended last ended."""
    r = await store.read(lambda c: c.execute(
        "SELECT ended_by FROM terminals WHERE name=? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC, rowid DESC LIMIT 1", (name,)).fetchone())
    return r[0] if r else None


async def end(store, id: str, ended_by: str, exit: int | None) -> None:
    """The terminal is gone: how, and its exit. Once."""
    await store.write(lambda c: c.execute(
        "UPDATE terminals SET ended_at=?, ended_by=?, exit=? WHERE id=? AND ended_at IS NULL",
        (now(), ended_by, exit, id)))


async def set_may_type(store, id: str, on: bool) -> None:
    await store.write(lambda c: c.execute(
        "UPDATE terminals SET may_type=? WHERE id=?", (int(bool(on)), id)))


async def trash_ended(store) -> int:
    """Every ended terminal into the trash. Returns how many."""
    return await store.write(lambda c: c.execute(
        "UPDATE terminals SET trashed_at=? WHERE ended_at IS NOT NULL AND trashed_at IS NULL",
        (now(),)).rowcount)


async def set_trashed(store, id: str, on: bool) -> bool:
    """An ended terminal into the trash, or any back out. False when there is no such
    row to change (an open one stays out of the trash)."""
    return await store.write(lambda c: c.execute(
        "UPDATE terminals SET trashed_at=? WHERE id=? AND (? = 0 OR ended_at IS NOT NULL)",
        (now() if on else None, id, int(on))).rowcount > 0)


# ---- its history: the raw output of each terminal, kept for good --------------------------

async def purge(store, data_dir: Path, id: str) -> bool:
    """A trashed terminal for good: its row, its commands and its stored history. False
    when it is not in the trash."""
    def fn(c: sqlite3.Connection) -> int:
        n = c.execute("DELETE FROM terminals WHERE id=? AND trashed_at IS NOT NULL",
                      (id,)).rowcount
        if n:
            c.execute("DELETE FROM terminal_commands WHERE terminal_id=?", (id,))
        return n

    gone = await store.write(fn)
    if not gone:
        return False
    raw = log_path(data_dir, id)
    if raw is not None:
        raw.unlink(missing_ok=True)
        packed(raw).unlink(missing_ok=True)
    return True


def log_path(data_dir: Path, id: str) -> Path | None:
    """<data dir>/terminals/<id>.raw, where the sync appends a terminal's output; None
    for an id that is no plain file name."""
    return data_dir / "terminals" / f"{id}.raw" if PLAIN_ID.fullmatch(id) else None


def packed(raw: Path) -> Path:
    return raw.with_name(raw.name + ".gz")


def pack(raw: Path) -> None:
    """An ended terminal's history into <id>.raw.gz. The .raw goes once the .gz is whole;
    a crash in between packs it again."""
    with open(raw, "rb") as src, gzip.open(packed(raw), "wb") as dst:
        shutil.copyfileobj(src, dst)
    raw.unlink()


def history(data_dir: Path, id: str) -> Iterator[bytes]:
    """The whole stored history in chunks: the .raw while there is one, else the .gz
    unzipped, else nothing."""
    raw = log_path(data_dir, id)
    if raw is None:
        return
    try:
        f = open(raw, "rb")
    except FileNotFoundError:
        try:
            f = gzip.open(packed(raw), "rb")
        except FileNotFoundError:
            return
    with f:
        while chunk := f.read(1 << 16):
            yield chunk


async def add_command(store, terminal_id: str, kind: str, text: str,
                      result: str | None = None, note: str | None = None) -> str:
    """A line the user typed, or a control they pressed. Returns its id."""
    cid = new_id()
    await store.write(lambda c: c.execute(
        "INSERT INTO terminal_commands(id, terminal_id, kind, text, result, note, "
        "created_at) VALUES(?,?,?,?,?,?,?)",
        (cid, terminal_id, kind, text, result, note, now())))
    return cid


async def last_conversation(store, terminal_id: str) -> dict | None:
    """The conversation of the newest shell call whose result names this terminal: {id,
    title, agent_of, kind (mission, sub_agent or conversation)}. Who typed last, for a
    command's words; opener() is who opened it, for whose it is and what closes it."""
    def fn(c: sqlite3.Connection) -> dict | None:
        r = c.execute(
            "SELECT c.id, c.title, c.agent_of, CASE WHEN mi.conversation_id IS NOT NULL "
            "THEN 'mission' WHEN c.agent_of IS NOT NULL THEN 'sub_agent' "
            "ELSE 'conversation' END AS kind FROM tool_calls tc "
            "JOIN messages a ON a.id = tc.message_id "
            "JOIN conversations c ON c.id = a.conversation_id "
            "LEFT JOIN missions mi ON mi.conversation_id = c.id "
            f"WHERE {SHELL_CALLS} AND json_valid(tc.display) "
            "AND json_extract(tc.display, '$.terminal_id') = ? "
            "ORDER BY tc.started_at DESC, tc.rowid DESC LIMIT 1", (terminal_id,)).fetchone()
        return dict(r) if r else None

    return await store.read(fn)


async def default_terminal(store, conversation_id: str) -> str:
    """Where a shell call with no terminal goes, and a browser call with no tab: the
    mission's terminal, a sub-agent's own name cut to fit and -<the first 4 characters of
    its id>, else the conversation's own, c-<the first 6 characters of its id>: two
    conversations never share one."""
    def fn(c: sqlite3.Connection) -> str:
        r = c.execute(
            "SELECT mi.terminal, c.agent_of, c.agent_name FROM conversations c "
            "LEFT JOIN missions mi ON mi.conversation_id = c.id WHERE c.id=?",
            (conversation_id,)).fetchone()
        if r is not None and r["terminal"]:
            return r["terminal"]
        if r is not None and r["agent_of"]:
            tail = "-" + slug(conversation_id)[:4]
            return (slug(r["agent_name"] or "")[:NAME_MAX - len(tail)].rstrip("-")
                    or "agent") + tail
        return "c-" + (slug(conversation_id)[:6] or "main")

    return await store.read(fn)


async def commands(store, terminal_id: str | None = None, kind: str | None = None,
                   q: str | None = None, mission: str | None = None, limit: int = 100,
                   before: str | None = None,
                   live: dict | None = None) -> tuple[list[dict], int]:
    """The agent's shell calls and the user's lines and controls as one list, newest first.
    `kind` is command, read, line or control; `q` is looked for in the text; `mission`
    keeps the rows of that conversation and of its sub-agents; `before` is the `at` of
    the last row of the previous page. The total counts what the filters match without
    the page. `live` is the sync's live state by terminal id: a call that returned
    while its program ran is settled with it."""
    where, args = [], []
    if terminal_id is not None:
        where.append("terminal_id = ?")
        args.append(terminal_id)
    if kind is not None:
        where.append("kind = ?")
        args.append(kind)
    if q:
        where.append("instr(lower(coalesce(text, '')), lower(?)) > 0")
        args.append(q)
    if mission is not None:
        where.append("(conversation_id = ? OR agent_of = ?)")
        args += [mission, mission]
    match = (" WHERE " + " AND ".join(where)) if where else ""
    page_where, page_args = match, list(args)
    if before:
        page_where += (" AND " if where else " WHERE ") + "at < ?"
        page_args.append(before)
    # each row with the next one in its terminal, and whether the terminal ended
    union = (f"WITH every AS ({_HERS} UNION ALL {_YOURS}), cmds AS ("
             "SELECT every.*, lead(id) OVER t AS next_id, lead(display) OVER t AS next_display, "
             "lead(text) OVER t AS next_text, "
             "(SELECT ended_at FROM terminals WHERE id = every.terminal_id) AS terminal_ended "
             "FROM every WINDOW t AS (PARTITION BY terminal_id ORDER BY at, id)) ")
    live = live or {}

    def fn(c: sqlite3.Connection) -> tuple[list[dict], int]:
        total = c.execute(f"{union}SELECT count(*) FROM cmds{match}", args).fetchone()[0]
        rows = [_command(r, live) for r in c.execute(
            f"{union}SELECT * FROM cmds{page_where} ORDER BY at DESC, id DESC LIMIT ?",
            [*page_args, limit])]
        return rows, int(total)

    return await store.read(fn)


def _one(c: sqlite3.Connection, where: str, args: tuple) -> dict | None:
    r = c.execute(f"SELECT * FROM terminals WHERE {where}", args).fetchone()
    return _view(r) if r else None


def _view(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["may_type"] = bool(d["may_type"])
    return d


def _command(r: sqlite3.Row, live: dict) -> dict:
    display = _dict(r["display"])
    outcome = None
    if r["kind"] == "line":         # a line the user typed: settled like its own
        outcome = {k: None for k in OUTCOME} | _settled(r, live.get(r["terminal_id"]))
    elif r["from_kind"] != "you":
        outcome = {k: (display or {}).get(k) for k in OUTCOME}
        outcome["state"] = outcome["state"] or r["state"]
        if display and display.get("state") in SNAPSHOT:
            outcome.update(running_for=None, quiet_for=None, paused=False,
                           **_settled(r, live.get(r["terminal_id"])))
    keys = r["keys"]
    if keys is not None and not isinstance(keys, str):
        keys = str(keys)
    return {"id": r["id"], "kind": r["kind"], "at": r["at"],
            "terminal": {"id": r["terminal_id"], "name": r["terminal_name"]},
            "from": {"kind": r["from_kind"], "conversation_id": r["conversation_id"],
                     "title": r["title"]},
            "text": r["text"] or "", "keys": keys or "", "outcome": outcome,
            "result": _screen(r["result"], (display or {}).get("head")), "note": r["note"],
            "job_id": r["job_id"]}


def _screen(result: str | None, head: int | None) -> str | None:
    """The agent's result without its lines above the screen: what the terminal printed."""
    if result is None or not isinstance(head, int):
        return result
    return "\n".join(result.split("\n")[head:]).strip("\n") or None


def _settled(r: sqlite3.Row, live: dict | None) -> dict:
    """How a call that returned while its program ran went since: over once anything
    came after it in the terminal or the terminal ended (a watch that came back done
    carries the exit), else what the terminal does now; running while nobody knows."""
    if r["next_id"] is not None:
        nxt = _dict(r["next_display"]) or {}
        if nxt.get("state") == "done" and not r["next_text"]:
            return {"state": "done", "exit": nxt.get("exit")}
        return {"state": "done", "exit": None}
    if r["terminal_ended"]:
        return {"state": "done", "exit": None}
    if live and not live.get("busy") and not live.get("program"):
        return {"state": "done", "exit": live.get("last_exit")}
    return {"state": "running", "exit": None}


def _dict(text: str | None) -> dict | None:
    try:
        d = json.loads(text) if text else None
    except ValueError:
        return None
    return d if isinstance(d, dict) else None
