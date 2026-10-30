"""Conversation rows: plain functions over the store."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import anyio

from .. import homes
from ..core.ids import iso, new_id, now
from ..core.states import LIVE_SQL
from ..kb.chat import fts_candidates, is_query_error
from . import tree
from ..core.event_types import (
    CONVERSATION_AUTO_CONTEXT,
    CONVERSATION_ARCHIVED,
    CONVERSATION_CREATED,
    CONVERSATION_DELETED,
    CONVERSATION_NOTES_OFF,
    CONVERSATION_PINNED,
    CONVERSATION_PURGED,
    CONVERSATION_RESTORED,
    CONVERSATION_TITLE,
    CONVERSATION_UNARCHIVED,
    IMAGES_NOTICE,
    MESSAGE_PURGED,
)

log = logging.getLogger("celine.conversations")


async def create(store, title: str = "", trigger_key: str | None = None,
                 agent_of: str | None = None, agent_name: str | None = None,
                 notebook_folder: str | None = None, access: str | None = None,
                 auto: bool = False, kind: str = "chat",
                 agent_cli: str | None = None, agent_device: str | None = None,
                 agent_template: str | None = None) -> str:
    """`kind` is what it is (chat, mission, background; a sub-agent's is always
    `agent`), fixed from here on. A sub-agent's conversation (sub_agents/skill.py)
    carries the conversation that started it, its name, the notebook folder it is
    limited to, its access (read or work), and the agent CLI that runs its briefs, when
    one does (sub_agents/cli.py), with the device it runs on (sub_agents/drone.py), and the
    agent template it was started from (sub_agents/templates.py)."""
    cid = new_id()
    kind = "agent" if agent_of else kind

    def fn(c: sqlite3.Connection) -> str:
        ts = now()
        c.execute(
            "INSERT INTO conversations(id, title, trigger_key, agent_of, agent_name, "
            "notebook_folder, access, agent_cli, agent_device, agent_template, title_auto, "
            "kind, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, title, trigger_key, agent_of, agent_name, notebook_folder,
             access, agent_cli, agent_device, agent_template, int(auto), kind, ts, ts),
        )
        announce(store, c, cid)
        return cid

    return await store.write(fn)


def announce(store, c: sqlite3.Connection, cid: str) -> None:
    """`conversation.created` on the global feed, from the row as it is written: every
    page's list adds it."""
    r = c.execute("SELECT id, title, agent_of, trigger_key, kind, created_at, active_leaf_id "
                  "FROM conversations WHERE id=?", (cid,)).fetchone()
    store.global_event(c, CONVERSATION_CREATED, {
        "id": r["id"], "title": r["title"], "agent_of": r["agent_of"],
        "trigger_key": r["trigger_key"], "kind": r["kind"], "created_at": r["created_at"],
        "last_line": last_line(c, r["active_leaf_id"])})


def trigger_conversation(store, c: sqlite3.Connection, trigger_key: str, title: str,
                         kind: str) -> tuple[str, bool]:
    """INSERT ... DO NOTHING, then SELECT: (its id, made by this call)."""
    ts, cid = now(), new_id()
    made = bool(c.execute(
        "INSERT INTO conversations(id, title, trigger_key, kind, created_at, "
        "updated_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(trigger_key) DO NOTHING",
        (cid, title, trigger_key, kind, ts, ts),
    ).rowcount)
    if made:
        announce(store, c, cid)
    row = c.execute(
        "SELECT id FROM conversations WHERE trigger_key = ?", (trigger_key,)
    ).fetchone()
    return str(row["id"]), made


async def for_trigger(store, trigger_key: str, title: str,
                      kind: str = "background") -> str:
    """The conversation of a trigger, made on its first fire. One transaction, so two
    overlapping fires of the same trigger cannot make two conversations."""
    return (await store.write(lambda c: trigger_conversation(
        store, c, trigger_key, title, kind)))[0]


async def update(store, cid: str, title: str | None = None,
                 pinned: bool | None = None, auto: bool = False,
                 notes_off: list[str] | None = None) -> bool:
    """`auto`: the title is the model's (after the first reply, turns/title.py), stored as
    it wrote it; the user's is stripped (its length is checked by the API). A changed `notes_off` (the notes left out here) is a `conversation.notes_off` event
    on the conversation's log."""
    def fn(c: sqlite3.Connection) -> bool:
        sets, args = [], []
        if notes_off is not None:
            row = c.execute("SELECT notes_off FROM conversations WHERE id=?",
                            (cid,)).fetchone()
            if row is not None and json.loads(row["notes_off"]) != notes_off:
                c.execute("UPDATE conversations SET notes_off=? WHERE id=?",
                          (json.dumps(notes_off), cid))
                store.event(c, cid, CONVERSATION_NOTES_OFF, {"notes_off": notes_off})
        if title is not None:
            sets.append("title=?, title_auto=?")
            args += [title if auto else title.strip(), int(auto)]
        if pinned is not None:
            sets.append("pinned=?")
            args.append(1 if pinned else 0)
        if not sets:
            return c.execute("SELECT 1 FROM conversations WHERE id=?",
                             (cid,)).fetchone() is not None
        args.append(cid)
        ok = c.execute(f"UPDATE conversations SET {', '.join(sets)} WHERE id=?",
                       args).rowcount == 1
        if ok and title is not None:
            store.event(c, cid, CONVERSATION_TITLE,
                        {"title": title if auto else title.strip()})
        if ok and pinned is not None:
            store.global_event(c, CONVERSATION_PINNED, {"id": cid,
                                                         "pinned": bool(pinned)})
        return ok

    return await store.write(fn)


def named(c: sqlite3.Connection, tz, first: str | None = None) -> list[dict]:
    """The agent's chats, `first` first then the newest, each with the name people read: a
    mission's name, a sub-agent's, the title the user gave it, else its name and the
    day it started (an app title is no name, nor an unnamed mission's brief start)."""
    from ..intercom.store import NAMES
    rows = c.execute(
        "SELECT c.id, c.title, c.title_auto, c.agent_name, c.created_at, "
        "mi.name AS mission, mi.brief "
        "FROM conversations c LEFT JOIN missions mi ON mi.conversation_id = c.id "
        "WHERE (c.agent_of IS NULL AND c.trashed_at IS NULL) OR c.id = ? "
        "ORDER BY c.id = ? DESC, c.pinned DESC, c.updated_at DESC, c.id DESC",
        (first, first)).fetchall()

    def name(r) -> str:
        if r["mission"] and r["mission"] != " ".join(r["brief"].split())[:40]:
            return r["mission"]
        if r["agent_name"]:
            return r["agent_name"]
        if r["title"] and not r["title_auto"]:
            return r["title"]
        d = datetime.fromisoformat(r["created_at"]).astimezone(tz)
        return f"{NAMES['her']} {d.day} {d:%b %H:%M}"

    return [{"id": r["id"], "name": name(r)} for r in rows]


async def title(store, cid: str) -> str | None:
    """The title, or None when there is no such conversation."""
    def fn(c: sqlite3.Connection) -> str | None:
        r = c.execute("SELECT title FROM conversations WHERE id=?", (cid,)).fetchone()
        return r["title"] if r else None

    return await store.read(fn)


async def first_exchange(store, cid: str) -> tuple[str, str]:
    """The first user message and the first assistant reply on the active path."""
    def fn(c: sqlite3.Connection) -> tuple[str, str]:
        chain = tree.path(c, cid)
        user = next((m["content"] for m in chain if m["role"] == "user"), "")
        reply = next((m["content"] for m in chain
                      if m["role"] == "assistant" and m["content"]), "")
        return user, reply

    return await store.read(fn)


async def delete(store, cid: str) -> bool:
    """A delete trashes the conversation and its sub-agents, all the way down (nothing is
    removed; `restore` brings them back), every kind alike. A trigger's conversation
    lets go of its key: the next fire makes a new one, and a restore brings this one
    back as a plain conversation. Refused while any of them has a live turn or a running
    job. -> whether it was there to trash."""
    def fn(c: sqlite3.Connection) -> bool:
        ids = family(c, cid)
        _refuse_busy(c, ids)
        ts = now()
        gone = c.execute("UPDATE conversations SET trashed_at=?, trigger_key=NULL WHERE id=? "
                         "AND trashed_at IS NULL", (ts, cid)).rowcount == 1
        if gone:
            for one in ids:
                c.execute("UPDATE conversations SET trashed_at=?, trigger_key=NULL WHERE "
                          "id=? AND trashed_at IS NULL", (ts, one))
                store.global_event(c, CONVERSATION_DELETED, {"id": one})
            from ..intercom.deliver import drop
            drop(c, store, ids, everyone=True)
        return gone

    return await store.write(fn)


async def restore(store, cid: str) -> bool:
    """A deleted conversation back, with the sub-agents trashed with it."""
    def fn(c: sqlite3.Connection) -> bool:
        r = c.execute("SELECT trashed_at FROM conversations WHERE id=?", (cid,)).fetchone()
        if r is None or r[0] is None:
            return False
        for one in family(c, cid):
            if c.execute("UPDATE conversations SET trashed_at=NULL WHERE id=? AND "
                         "trashed_at=?", (one, r[0])).rowcount:
                store.global_event(c, CONVERSATION_RESTORED, {"id": one})
        return True

    return await store.write(fn)


# ---- delete permanently: a conversation from the Trash, a message at once; only by the user

NOT_TRASHED = "It is not in the Trash."
# references that link a row to one of another owner, not part of it: cleared, the row
# stays (a message in its chat that landed a sub-agent's report)
_LINKS = {("messages", "intercom_id"), ("intercom", "reply_to"), ("intercom", "thread")}
# columns that name what a row belongs to
_OWNERS = ("conversation_id",)


def _tables(c: sqlite3.Connection) -> list[str]:
    """The app's own tables (no full-text shadow tables, no sqlite_*)."""
    return [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE '%_fts%' AND sql NOT LIKE 'CREATE VIRTUAL%'")]


def _columns(c: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


def _children(c: sqlite3.Connection, parent: str) -> list[tuple[str, str, str, str]]:
    """(table, column, parent column, on delete) of every reference to `parent`."""
    out = []
    for t in _tables(c):
        for r in c.execute(f"PRAGMA foreign_key_list({t})"):
            if r[2] == parent:
                out.append((t, r[3], r[4] or "rowid", r[6]))
    return out


def _drop(c: sqlite3.Connection, table: str, column: str, values: list) -> None:
    """Every row of `table` whose `column` is one of `values`, and before it every row
    that is part of one (its references, all the way down); a link from elsewhere is
    cleared instead. The foreign keys are checked at the commit (defer_foreign_keys)."""
    values = [*dict.fromkeys(v for v in values if v is not None)]   # (list is store.list)
    if not values:
        return
    marks = ",".join("?" * len(values))
    for child, ccol, pcol, action in _children(c, table):
        keys = [r[0] for r in c.execute(
            f"SELECT {pcol} FROM {table} WHERE {column} IN ({marks})", values)]
        if not keys:
            continue
        kmarks = ",".join("?" * len(keys))
        if child == table or (child, ccol) in _LINKS or action == "SET NULL":
            c.execute(f"UPDATE {child} SET {ccol}=NULL WHERE {ccol} IN ({kmarks})", keys)
        else:
            _drop(c, child, ccol, keys)
    c.execute(f"DELETE FROM {table} WHERE {column} IN ({marks})", values)


def _drop_kv(c: sqlite3.Connection, names: set[str]) -> None:
    """The kv rows kept for one of `names` (keys like keep_time:<turn id>)."""
    for (k,) in c.execute("SELECT k FROM kv").fetchall():
        if any(part in names for part in k.split(":")):
            c.execute("DELETE FROM kv WHERE k=?", (k,))


def purge_sync(c: sqlite3.Connection, store, cid: str) -> list[str] | None:
    """Inside a write: a conversation in the Trash removed for good, with its sub-agents
    and every row that is part of them (its messages, turns, calls, contexts, a
    mission's, the intercom to and from it, the attachments of its messages);
    `conversation.purged` for each. A link to it from elsewhere is cleared (a run, a
    report's address). -> the ids removed; None: no such conversation.
    ValueError(NOT_TRASHED): it is not in the Trash. tree.Busy while any of them works."""
    row = c.execute("SELECT trashed_at FROM conversations WHERE id=?", (cid,)).fetchone()
    if row is None:
        return None
    if row[0] is None:
        raise ValueError(NOT_TRASHED)
    ids = family(c, cid)
    _refuse_busy(c, ids)
    c.execute("PRAGMA defer_foreign_keys=ON")
    marks = ",".join("?" * len(ids))
    mids = [r[0] for r in c.execute(
        f"SELECT id FROM messages WHERE conversation_id IN ({marks})", ids)]
    tids = [r[0] for r in c.execute(
        f"SELECT id FROM turns WHERE conversation_id IN ({marks})", ids)]
    addrs = [f"agent:{x}" for x in ids]
    amarks = ",".join("?" * len(addrs))
    _drop(c, "intercom", "id", [r[0] for r in c.execute(
        f"SELECT id FROM intercom WHERE from_addr IN ({amarks}) OR to_addr IN ({amarks})",
        addrs + addrs)])
    # log.db has no foreign keys into this file: its rows go by name
    _drop(c, "requests", "message_id", mids)
    _drop(c, "requests", "turn_id", tids)
    _drop(c, "raw_responses", "message_id", mids)
    _drop(c, "events", "conversation_id", ids)
    # a row that only names it (ON DELETE SET NULL: a run, a bug report) stays; the
    # conversations drop below clears the name
    named = {(t, col) for t, col, _, act in _children(c, "conversations") if act == "SET NULL"}
    for t in _tables(c):
        if t == "conversations":
            continue
        cols = _columns(c, t)
        for col in _OWNERS:
            if col in cols and (t, col) not in named:
                _drop(c, t, col, ids)
    _drop_kv(c, {*ids, *mids, *tids})
    _drop(c, "conversations", "id", ids[::-1])
    for one in ids:
        store.global_event(c, CONVERSATION_PURGED, {"id": one})
    return ids


async def purge(store, cfg, cid: str) -> list[str] | None:
    """purge_sync in its own write, then the request files kept for them."""
    ids = await store.write(lambda c: purge_sync(c, store, cid))
    for one in ids or ():
        (cfg.data_dir / "requests" / f"{one}.json").unlink(missing_ok=True)
    return ids


def delete_message_sync(c: sqlite3.Connection, store, cid: str, mid: str) -> dict:
    """Inside a write: a message removed for good with everything after it in its branch
    (the replies, tool results and versions below it); the path goes on from its parent,
    or from another version of it. `message.purged` with the ids removed and the active
    leaf. KeyError: no such message here; tree.Busy while a turn of the conversation is
    live."""
    m = c.execute("SELECT parent_id FROM messages WHERE id=? AND conversation_id=?",
                  (mid, cid)).fetchone()
    if m is None:
        raise KeyError(mid)
    tree.refuse_if_live(c, cid)
    c.execute("PRAGMA defer_foreign_keys=ON")
    gone = [r[0] for r in c.execute(
        "WITH RECURSIVE sub(id) AS (SELECT ? UNION ALL SELECT x.id FROM messages x "
        "JOIN sub ON x.parent_id = sub.id) SELECT id FROM sub", (mid,))]
    parent = m["parent_id"]
    leaf = tree.leaf(c, cid)
    _drop(c, "requests", "message_id", gone)
    _drop(c, "raw_responses", "message_id", gone)
    _drop_kv(c, set(gone))
    _drop(c, "messages", "id", gone)
    if leaf in gone:
        if parent is None:
            root = c.execute("SELECT id FROM messages WHERE conversation_id=? AND parent_id "
                             "IS NULL ORDER BY ordinal DESC, created_at DESC LIMIT 1",
                             (cid,)).fetchone()
            parent = root[0] if root else None
        leaf = tree._deepest_leaf(c, parent) if parent else None
        tree.set_leaf(c, cid, leaf)
    store.event(c, cid, MESSAGE_PURGED, {"message_ids": gone, "active_leaf_id": leaf})
    return {"message_ids": gone, "active_leaf_id": leaf}


def family(c: sqlite3.Connection, cid: str) -> list[str]:
    """`cid` and its sub-agents, all the way down, parents first."""
    return [r[0] for r in c.execute(
        "WITH RECURSIVE fam(id) AS (SELECT ? UNION ALL SELECT s.id FROM conversations s "
        "JOIN fam ON s.agent_of = fam.id) SELECT id FROM fam", (cid,))]


def _refuse_busy(c: sqlite3.Connection, ids: list[str], users_only: bool = False) -> None:
    """tree.Busy while any of `ids` has a live turn or a running job; `users_only`:
    only while a turn of the user's is live."""
    marks = ",".join("?" * len(ids))
    live = c.execute(f"SELECT 1 FROM turns WHERE conversation_id IN ({marks}) AND "
                     f"state IN {LIVE_SQL}" + (" AND origin='user'" if users_only else "")
                     + " LIMIT 1", ids).fetchone()
    if live or not users_only and c.execute(
            f"SELECT 1 FROM jobs WHERE conversation_id IN ({marks}) AND "
            f"state='running' LIMIT 1", ids).fetchone():
        raise tree.Busy()      # a running job would land its result in a hole


# Settings > General "Lock archived chats". A locked conversation takes nothing it
# does on its own: no turn of its own starts or goes on there (turns/store.claim), nothing
# but the user's own messages lands there (intercom/deliver.py), its intercom refuses to
# write there (intercom/addresses.py) and its reviews leave it out. A deleted one is
# always locked. Every reader is SQL in a transaction, so the switch is in the database:
# kv LOCK_KEY, written from the setting whenever the settings load (reload.apply, the
# start), on ("1") until then.
LOCK_KEY = "settings.lock_archived"
_LOCK_ON = f"coalesce((SELECT v FROM kv WHERE k='{LOCK_KEY}'), '1') = '1'"
ARCHIVED = "That chat is archived."
DELETED = "That chat is deleted."


def set_lock_archived(c: sqlite3.Connection, on: bool) -> None:
    c.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
              (LOCK_KEY, "1" if on else "0"))


def keep_lock(store, cfg) -> None:
    """The setting into kv, where the SQL reads it; queued, so it holds for every write
    after it."""
    from ..settings import general
    on = bool(general.value(cfg.settings.general, "lock_archived"))
    store.write_soon(lambda c: set_lock_archived(c, on))


def lock_archived(c: sqlite3.Connection) -> bool:
    return bool(c.execute(f"SELECT {_LOCK_ON}").fetchone()[0])


def locked_sql(a: str = "c") -> str:
    """SQL: conversation row `a` is locked."""
    return f"({a}.trashed_at IS NOT NULL OR ({a}.archived_at IS NOT NULL AND {_LOCK_ON}))"


def locked(c: sqlite3.Connection, cid: str) -> str | None:
    """Why conversation `cid` is locked, in one line, or None when it is not."""
    r = c.execute("SELECT trashed_at, archived_at FROM conversations WHERE id=?",
                  (cid,)).fetchone()
    if r is None:
        return None
    if r["trashed_at"]:
        return DELETED
    return ARCHIVED if r["archived_at"] and lock_archived(c) else None


def depth(c: sqlite3.Connection, cid: str) -> int:
    """How many levels `cid` is below the top of its agent_of chain: 0 for a
    conversation that is not a sub-agent."""
    n, seen = 0, {cid}
    while (r := c.execute("SELECT agent_of FROM conversations WHERE id=?",
                          (cid,)).fetchone()) and r["agent_of"] and r["agent_of"] not in seen:
        cid = r["agent_of"]
        seen.add(cid)
        n += 1
    return n


def set_archived(c: sqlite3.Connection, store, cid: str, archived: bool) -> bool:
    """Inside a write: archive or unarchive the conversation `cid` belongs to (a
    sub-agent's is its top conversation) with all its sub-agents, and say so on the
    global feed. Nothing is removed. Archiving is refused (tree.Busy) while any of them
    works; with Lock archived chats only while a turn of the user's is live, and what
    waits to land there from anyone else is dropped (its running work is stopped by the
    caller, conversations/api.py). -> whether anything changed."""
    top = (homes.root(c, cid) or {"id": cid})["id"]
    row = c.execute("SELECT archived_at FROM conversations WHERE id=?", (top,)).fetchone()
    if row is None or (row["archived_at"] is not None) == archived:
        return False
    ids = family(c, top)
    lock = lock_archived(c)
    if archived:
        _refuse_busy(c, ids, users_only=lock)
    ts = now() if archived else None
    c.execute(f"UPDATE conversations SET archived_at=? WHERE id IN "
              f"({','.join('?' * len(ids))})", (ts, *ids))
    if archived:
        store.global_event(c, CONVERSATION_ARCHIVED, {"id": top, "archived_at": ts})
        if lock:
            from ..intercom.deliver import drop
            drop(c, store, ids)
    else:
        store.global_event(c, CONVERSATION_UNARCHIVED, {"id": top})
    return True


async def archive(store, cid: str, archived: bool = True) -> dict | None:
    """The user's archive: the conversation and its sub-agents leave the list (or come
    back), every message kept. -> {archived_at}, None for an unknown conversation."""
    def fn(c: sqlite3.Connection) -> dict | None:
        if c.execute("SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone() is None:
            return None
        set_archived(c, store, cid, archived)
        return {"archived_at": c.execute("SELECT archived_at FROM conversations WHERE "
                                         "id=?", (cid,)).fetchone()[0]}

    return await store.write(fn)


async def ensure_writable(store, cid: str) -> None:
    """409 with the sentence when the conversation was deleted (restore it first): it
    stays readable, nothing new is written in it. An archived one comes back: what the
    user does there unlocks it (locked)."""
    trashed = await store.read(lambda c: (c.execute(
        "SELECT trashed_at FROM conversations WHERE id=?", (cid,)).fetchone() or [None])[0])
    if trashed:
        from ..errors import CelineError
        raise CelineError("deleted", "This conversation is deleted. Restore it to go on.",
                          409)
    await archive(store, cid, False)


def last_line(c: sqlite3.Connection, leaf_id: str | None) -> str:
    """The first line of the newest message's text on the active path (its own or the
    user's; tool results are passed over), at most 120 characters; "" when that message
    has no text."""
    mid, seen = leaf_id, set()
    while mid and mid not in seen:
        seen.add(mid)
        m = c.execute("SELECT parent_id, role, content FROM messages WHERE id=?",
                      (mid,)).fetchone()
        if m is None:
            break
        if m["role"] in ("user", "assistant"):
            text = (m["content"] or "").strip()
            return text.splitlines()[0].strip()[:120] if text else ""
        mid = m["parent_id"]
    return ""


_SUMMARY = (
    "SELECT c.id, c.title, c.pinned, c.trigger_key, c.agent_of, c.kind, "
    "       c.access, "
    "       c.updated_at, "
    "       c.active_leaf_id, "
    "       c.archived_at, c.next_seq - 1 AS seq, "
    "       (SELECT state FROM turns t WHERE t.conversation_id = c.id "
    f"         AND t.state IN {LIVE_SQL} "
    "         ORDER BY t.created_at LIMIT 1) AS active_state, "
    "       mi.name AS mission_name, mi.status AS mission_status "
    "FROM conversations c LEFT JOIN missions mi ON mi.conversation_id = c.id ")


# a turn that waits in the queue while Bastet holds her frozen (guard/freeze.py) does
# not run: its conversation is not under Running and its row has no step
_HELD = "EXISTS (SELECT 1 FROM kv WHERE k = 'frozen')"
# Running first (a live turn in it or in a sub-agent under it), then pinned, then the rest
_RANK = (f"CASE WHEN EXISTS (WITH RECURSIVE fam(id) AS (SELECT c.id UNION ALL SELECT "
         f"s.id FROM conversations s JOIN fam ON s.agent_of = fam.id) SELECT 1 FROM turns t "
         f"JOIN fam ON fam.id = t.conversation_id WHERE t.state IN {LIVE_SQL} "
         f"AND NOT (t.state = 'queued' AND {_HELD})) "
         f"THEN 2 WHEN c.pinned THEN 1 ELSE 0 END")
# the step words of a live turn ('' : the dots); a running tool says its label
_STEP = {"queued": "", "preparing": "Thinking", "streaming": ""}


def group_of(rank: int, updated_at: str, tz, now_: datetime) -> str:
    """The list's group label of a row: Running, Pinned, then the day it was last
    active in the app's time zone (Today, Yesterday, the weekday, "9 Oct", "October
    2025")."""
    if rank == 2:
        return "Running"
    if rank == 1:
        return "Pinned"
    d = datetime.fromisoformat(updated_at).astimezone(tz).date()
    today = now_.astimezone(tz).date()
    days = (today - d).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return d.strftime("%A")
    return f"{d.day} {d:%b}" if d.year == today.year else d.strftime("%B %Y")


def state_of(c: sqlite3.Connection, row: dict) -> dict | None:
    """How its turn in it stands, in the server's words: running with its step (not
    while frozen), waiting for you (a mission), or how the last turn ended when it
    failed or paused."""
    from ..settings.tools_meta import TOOLS, humanize
    held = row["active_state"] == "queued" and c.execute(f"SELECT {_HELD}").fetchone()[0]
    if row["active_state"] and not held:
        words = _STEP.get(row["active_state"])
        if words is None:       # tools: the running call's label
            r = c.execute(
                "SELECT tc.name FROM tool_calls tc JOIN messages m ON m.id = tc.message_id "
                "JOIN turns t ON t.id = m.turn_id WHERE t.conversation_id = ? "
                f"AND t.state IN {LIVE_SQL} AND tc.state = 'running' "
                "ORDER BY tc.started_at DESC LIMIT 1", (row["id"],)).fetchone()
            words = (TOOLS.get(r[0], {}).get("label") or humanize(r[0])) if r else "Thinking"
        return {"tone": "running", "words": words}
    if row.get("mission_status") == "needs_you":
        return {"tone": "warn", "words": "Waiting for you"}
    last = c.execute("SELECT state FROM turns WHERE conversation_id = ? "
                     "ORDER BY created_at DESC, rowid DESC LIMIT 1", (row["id"],)).fetchone()
    if last and last[0] in ("failed", "interrupted"):
        return {"tone": "error", "words": "The reply failed."}
    if last and last[0] == "capped":
        return {"tone": "warn", "words": "Paused."}
    return None


async def list(store, limit: int = 200,  # noqa: A001 - the module's word for it
               after: tuple[int, str, str] | None = None,
               archived: bool = False, trashed: bool = False, *, place: str | None = None,
               tz=timezone.utc, now_: datetime | None = None) -> list[dict]:
    """Running first (a live turn in it or a sub-agent of it), then pinned, then the most
    recently updated. `after`: the (rank, updated_at, id) of the last row of the
    previous page. `archived`: only the archived ones, else only the others;
    `trashed`: only the deleted ones (archived or not). `place` "celine": only its
    chats (no missions). Background conversations are not in it (GET /api/background-conversations).
    A mission's row carries its name and status, every row its kind, its `last_line`
    (last_line above), `group` (group_of), `state` (state_of), `home` and `link`
    (homes.of: where it opens). Sub-agents are not rows of their own: each row lists its
    sub-agents in `agents`, oldest first, in the same shape; `rank` is the page's sort key (2 running,
    1 pinned, 0)."""
    now_ = now_ or datetime.now(timezone.utc)

    def fn(c: sqlite3.Connection) -> list[dict]:
        where = _shelf(archived, trashed, place)
        args: list = []
        page = ""
        if after is not None:
            p, u, i = after
            page = ("WHERE (rank < ? OR (rank = ? AND (updated_at < ? OR "
                    "(updated_at = ? AND id < ?)))) ")
            args = [p, p, u, u, i]
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM (" + _SUMMARY.replace("SELECT ", f"SELECT {_RANK} AS rank, ", 1)
            + where + ") " + page + "ORDER BY rank DESC, updated_at DESC, id DESC "
            "LIMIT ?", (*args, limit))]
        for r in rows:
            r["agents"] = [{**dict(s), "agents": []} for s in c.execute(
                _SUMMARY + "WHERE c.agent_of = ? ORDER BY c.created_at, c.id", (r["id"],))]
            r["group"] = group_of(r["rank"], r["updated_at"], tz, now_)
            for one in (r, *r["agents"]):
                one["last_line"] = last_line(c, one.pop("active_leaf_id"))
                one["state"] = state_of(c, one)
                one.update(homes.of(c, one["id"]))       # where it opens, the server's
            for a in r["agents"]:
                a["group"] = r["group"]
        return rows

    return await store.read(fn)


def _shelf(archived: bool, trashed: bool, place: str | None) -> str:
    where = "WHERE c.agent_of IS NULL AND c.kind IN ('chat', 'mission') " + (
        "AND c.trashed_at IS NOT NULL " if trashed else
        "AND c.trashed_at IS NULL AND c.archived_at IS " +
        ("NOT NULL " if archived else "NULL "))
    return where + ("AND c.kind = 'chat' " if place == "celine" else "")


async def count(store, archived: bool = False, trashed: bool = False,
                place: str | None = None) -> int:
    """How many rows `list` has on that shelf (the head's count)."""
    return await store.read(lambda c: c.execute(
        "SELECT count(*) FROM conversations c " + _shelf(archived, trashed, place)
    ).fetchone()[0])


async def background(store) -> list[dict]:
    """The background conversations but the runs' (`run:<id>`, they are in
    Routines), newest activity first: {id, title, kind, trigger (a fired trigger's name,
    from `trigger:<name>`), working, last_line, updated_at}."""
    def fn(c: sqlite3.Connection) -> list[dict]:
        rows = c.execute(_SUMMARY + "WHERE c.kind = 'background' AND "
                         "coalesce(c.trigger_key, '') NOT LIKE 'run:%' "
                         "ORDER BY c.updated_at DESC, c.id DESC").fetchall()
        out = []
        for r in rows:
            kind, _, rest = (r["trigger_key"] or "").partition(":")
            out.append({"id": r["id"], "title": r["title"], "kind": r["kind"],
                        "trigger": rest or None if kind == "trigger" else None,
                        "working": r["active_state"] is not None,
                        "last_line": last_line(c, r["active_leaf_id"]),
                        "updated_at": r["updated_at"]})
        return out

    return await store.read(fn)


def marked(snippet: str) -> dict:
    """An FTS snippet with its matches between \\x02 and \\x03 -> the plain `snippet` and
    the `marks`, [start, end) offsets of each match in it."""
    out, marks, start = [], [], 0
    for ch in snippet:
        if ch == "\x02":
            start = len(out)
        elif ch == "\x03":
            marks.append([start, len(out)])
        else:
            out.append(ch)
    return {"snippet": "".join(out), "marks": marks}


def titled(c: sqlite3.Connection, query: str, limit: int, archived: bool = False,
           trashed: bool = False, place: str | None = None) -> list[str]:
    """The ids of the conversations on that shelf (list) whose title holds every word of
    `query` (ASCII case folded), a title that starts with it first, then the most
    recently updated."""
    words = query.lower().split()
    if not words:
        return []
    has = " AND ".join("instr(lower(c.title), ?) > 0" for _ in words)
    return [r[0] for r in c.execute(
        "SELECT c.id FROM conversations c " + _shelf(archived, trashed, place) +
        f"AND {has} ORDER BY instr(lower(c.title), ?) = 1 DESC, c.updated_at DESC LIMIT ?",
        (*words, query.lower().strip(), limit))]


async def search(store, query: str, exclude: str | None = None, limit: int = 20,
                 snippet_tokens: int = 48, kinds: tuple[str, ...] | None = None) -> list[dict]:
    """FTS over user and assistant messages on each conversation's active path; `kinds`:
    only conversations of those kinds. The query as written, then its bare words, as the
    Messages archive does."""
    def fn(c: sqlite3.Connection) -> list[dict]:
        of = f"AND c.kind IN ({', '.join('?' * len(kinds))}) " if kinds else ""
        for match in fts_candidates(query):
            try:
                rows = c.execute(
                    "WITH RECURSIVE active(id, conversation_id) AS ("
                    "  SELECT active_leaf_id, id FROM conversations"
                    "    WHERE active_leaf_id IS NOT NULL"
                    "  UNION ALL"
                    "  SELECT m.parent_id, a.conversation_id FROM messages m"
                    "    JOIN active a ON m.id = a.id WHERE m.parent_id IS NOT NULL)"
                    "SELECT m.conversation_id, m.id AS message_id, c.title, c.archived_at,"
                    "  m.role, m.author, m.created_at,"
                    "  snippet(messages_fts, 0, char(2), char(3), '...', ?) AS snippet"
                    "  FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid"
                    "  JOIN active a ON a.id = m.id"
                    "  JOIN conversations c ON c.id = m.conversation_id"
                    "  WHERE messages_fts MATCH ? AND m.role IN ('user','assistant')"
                    "    AND c.trashed_at IS NULL"
                    "    AND m.conversation_id != ? " + of +
                    "  ORDER BY rank LIMIT ?",
                    (snippet_tokens, match, exclude or "", *(kinds or ()), limit)).fetchall()
            except sqlite3.OperationalError as e:
                if not is_query_error(e):
                    raise
                continue
            if rows:
                return [{**dict(r), **marked(r["snippet"])} for r in rows]
        return []

    return await store.read(fn)


async def note_images(store, cid: str, model: str, describer: str | None) -> None:
    """The line after the newest message when the conversation's model changes to one
    that cannot see images (event `images.notice`): `describer` writes the
    descriptions of the earlier images, or None when no model can."""
    def fn(c: sqlite3.Connection) -> None:
        leaf = tree.leaf(c, cid)
        if leaf:
            store.event(c, cid, IMAGES_NOTICE, {
                "after_message_id": leaf, "model": model, "describer": describer})

    await store.write(fn)


GLOBAL_KEEP = 20000     # rows of the global feed kept by prune_events (events.added
                        # adds one per tool call, turn and job end)
PRUNE_EVERY = 600.0     # seconds between two prunes of the app's loop (keep_pruned)


async def keep_pruned(store, cfg, every: float = PRUNE_EVERY) -> None:
    """The app's loop: the event logs pruned every `every` seconds, with events_keep as
    it is then (the startup prunes once before). Off the turns' path: a large prune
    holds up the writes behind it once in a while, not at every turn's end."""
    while True:
        await anyio.sleep(every)
        try:
            await prune_events(store, int(cfg.r("events_keep")),
                               requests_days=int(cfg.r("requests_keep")))
        except Exception:
            log.exception("prune failed", extra={"event": "events.prune_failed"})


async def prune_events(store, keep: int, cid: str | None = None,
                       requests_days: int = 0) -> int:
    """Drop all but the last `keep` events of every idle conversation, except what the
    snapshot reads back: the transcript's lasting lines (image notices) and which
    replies another model answered (`llm.queue` with `fallback`). A client resuming
    from below trim_below gets a resync instead of a gap. The global feed keeps its
    newest GLOBAL_KEEP rows the same way (a client below the oldest gets a resync).
    `requests_days` (the requests_keep setting): the request bodies in log.db older
    than that many days go too; 0 keeps them."""
    def fn(c: sqlite3.Connection) -> int:
        c.execute("DELETE FROM global_events WHERE id <= "
                  "(SELECT max(id) FROM global_events) - ?", (GLOBAL_KEEP,))
        if requests_days > 0:
            since = datetime.fromisoformat(now()) - timedelta(days=requests_days)
            c.execute("DELETE FROM requests WHERE created_at < ?", (iso(since),))
        sql = ("SELECT id, next_seq FROM conversations WHERE id NOT IN "
               f"(SELECT conversation_id FROM turns WHERE state IN {LIVE_SQL})")
        args: list = []
        if cid:
            sql += " AND id=?"
            args.append(cid)
        dropped = 0
        for row in c.execute(sql, args).fetchall():
            floor = int(row["next_seq"]) - keep
            if floor <= 1:
                continue
            dropped += c.execute(
                "DELETE FROM events WHERE conversation_id=? AND seq < ? "
                "AND type != 'images.notice' AND NOT (type = 'llm.queue' AND "
                "data LIKE '%\"fallback\": true%')", (row["id"], floor)).rowcount
            c.execute("UPDATE conversations SET trim_below=? WHERE id=? "
                      "AND trim_below < ?", (floor, row["id"], floor))
        return dropped

    return await store.write(fn)


def set_auto_context(c: sqlite3.Connection, store, cid: str, on: bool) -> None:
    """Inside a write: the Load automatically switch for `cid` and its sub-agents, each
    change on the feed."""
    for one in family(c, cid):
        if c.execute("UPDATE conversations SET auto_context=? WHERE id=? AND "
                     "auto_context IS NOT ?", (int(on), one, int(on))).rowcount:
            store.global_event(c, CONVERSATION_AUTO_CONTEXT, {"id": one, "on": on})


def inherit_auto_context(c: sqlite3.Connection, parent: str, sub: str) -> None:
    """A sub-agent's switch is its parent's (sub_agents/skill.py)."""
    c.execute("UPDATE conversations SET auto_context=(SELECT auto_context FROM conversations "
              "WHERE id=?) WHERE id=?", (parent, sub))


def set_settings(c: sqlite3.Connection, cid: str, clean: dict) -> bool:
    """A validated (partial) settings object (settings/conversations.py): a key that is
    absent stays, null resets it. False: no such conversation."""
    if c.execute("SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone() is None:
        return False
    if "model" in clean:
        c.execute("UPDATE conversations SET model_id=? WHERE id=?", (clean["model"], cid))
    if "thinking" in clean:
        c.execute("UPDATE conversations SET thinking=? WHERE id=?", (clean["thinking"], cid))
    if "guard" in clean:
        c.execute("UPDATE conversations SET guard_off=? WHERE id=?",
                  (int(not clean["guard"]), cid))
    return True
