"""The message tree of a conversation: every write to it, and the active path.
`append` is how a user-role message enters it."""
from __future__ import annotations

import json
import sqlite3

from ..core.ids import new_id, now
from .. import errors
from ..errors import mask, mask_all
from ..core.states import LIVE_SQL, QUEUE_ORDER
from .. import attachments as files
from ..intercom.store import insert as intercom_insert, view
from ..core.event_types import (
    MESSAGE_CREATED,
    MESSAGE_DELTA,
    MESSAGE_DONE,
    MESSAGE_PURGED,
    MESSAGE_REOPENED,
    MESSAGE_REWRITTEN,
    TURN_STATE,
    VERSION_CHANGED,
)


class Busy(errors.Busy):
    """Raised inside a write when the conversation still has a turn or a job running: a
    409 as it is (errors.py), a route with its own sentence catches it."""

    def __init__(self, message: str = "{{name}} is still answering."):
        super().__init__(message)


class Unresolved(errors.UnresolvedToolCalls):
    """Raised inside a write when tool calls on the target have not settled (409)."""

    def __init__(self, names: list[str]):
        super().__init__(names)
        self.names = names


def leaf(c: sqlite3.Connection, cid: str) -> str | None:
    """The active leaf; None when the conversation is empty or does not exist."""
    r = c.execute("SELECT active_leaf_id FROM conversations WHERE id=?", (cid,)).fetchone()
    return r["active_leaf_id"] if r else None


def set_leaf(c: sqlite3.Connection, cid: str, mid: str | None) -> None:
    c.execute("UPDATE conversations SET active_leaf_id=? WHERE id=?", (mid, cid))


def prefer(c: sqlite3.Connection, parent: str, child: str | None) -> None:
    """The version of `parent`'s children the path goes through (None: its newest)."""
    c.execute("UPDATE messages SET preferred_child_id=? WHERE id=?", (child, parent))


def follow(c: sqlite3.Connection, cid: str, parent: str, mid: str) -> None:
    """`mid` under `parent` is the visible path now: the parent prefers it, it is the
    leaf."""
    prefer(c, parent, mid)
    set_leaf(c, cid, mid)


def next_ordinal(c: sqlite3.Connection, parent: str | None) -> int:
    """The ordinal of a new child of `parent` (its siblings are the versions)."""
    return c.execute("SELECT coalesce(max(ordinal), -1) + 1 FROM messages WHERE "
                     "parent_id IS ?", (parent,)).fetchone()[0]


def insert_tool_result(c: sqlite3.Connection, mid: str, cid: str, parent: str, text: str,
                       ordinal: int, ts: str) -> dict:
    """A call's result row under the reply that made the call, said by the same speaker
    (its meta). -> that meta as clients read it (meta_view)."""
    meta = unsigned(c.execute("SELECT meta FROM messages WHERE id=?",
                              (parent,)).fetchone()["meta"])
    c.execute("INSERT INTO messages(id,conversation_id,parent_id,role,content,ordinal,"
              "author,created_at,meta) VALUES(?,?,?,'tool',?,?,'model',?,?)",
              (mid, cid, parent, text, ordinal, ts, meta))
    return meta_view(meta, "tool")


def delete_row(c: sqlite3.Connection, mid: str) -> None:
    c.execute("DELETE FROM messages WHERE id=?", (mid,))


def keep_time(c: sqlite3.Connection, cid: str, ts: str) -> None:
    """The conversation's place in the list back to `ts` (a continued reply)."""
    c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (ts, cid))


def refuse_if_live(c: sqlite3.Connection, cid: str) -> None:
    """Nothing outside the runner changes the tree while a turn of it is live."""
    if c.execute(f"SELECT 1 FROM turns WHERE conversation_id=? AND state IN {LIVE_SQL} "
                 "LIMIT 1", (cid,)).fetchone():
        raise Busy()


def _say_if_new_version(c: sqlite3.Connection, store, cid: str, parent: str | None,
                     mid: str, old_leaf: str | None) -> None:
    """A message that does not continue the visible path (an edit, a retry) starts a
    new version: the server says so, and every client fetches the path from the server
    instead of guessing it from message.created."""
    if old_leaf is not None and parent != old_leaf:
        store.event(c, cid, VERSION_CHANGED, {"parent_id": parent,
                                               "preferred_child_id": mid,
                                               "active_leaf_id": mid})


def meta_view(raw: str | None, role: str, quiet=False) -> dict:
    """A message's meta as clients and the context builder read it, from what is stored
    (messages.meta, only what is not the default): `quiet` (a reply of a quiet turn:
    only its tool calls show)."""
    return {"quiet": bool(quiet) and role == "assistant"}


def unsigned(raw: str | None) -> str | None:
    """A stored meta for another message (an edited or continued version, a tool result
    row) without the reply's signed thinking: its signature belongs to the old text, and
    the API refuses a request that sends it with other text."""
    m = json.loads(raw) if raw else {}
    m.pop("thinking", None)
    return json.dumps(m) if m else None


def append(c: sqlite3.Connection, store, cid: str, parent: str | None, role: str,
           text: str, author: str, intercom_id: int | None = None,
           turn_id: str | None = None, attachments=(), meta: str | None = None,
           mid: str | None = None) -> dict:
    """One message under `parent`, which becomes the active leaf, inside a write. The
    draft attachments given are bound to it; `meta` is stored as given (meta_view).
    `mid`: its id, when it was given out before. -> the message as the snapshot shows
    it."""
    mid, ts = mid or new_id(), now()
    old_leaf = leaf(c, cid)
    ordinal = c.execute(
        "SELECT coalesce(max(ordinal), -1) + 1 FROM messages WHERE parent_id IS ? "
        "AND conversation_id = ?", (parent, cid)).fetchone()[0]
    c.execute(
        "INSERT INTO messages(id,conversation_id,parent_id,role,content,ordinal,turn_id,"
        "author,intercom_id,created_at,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (mid, cid, parent, role, text, ordinal, turn_id, author, intercom_id, ts, meta))
    quiet = turn_id is not None and bool((c.execute(
        "SELECT quiet FROM turns WHERE id=?", (turn_id,)).fetchone() or [0])[0])
    if parent:
        c.execute("UPDATE messages SET preferred_child_id=? WHERE id=?", (mid, parent))
    c.execute("UPDATE conversations SET active_leaf_id=? WHERE id=?", (mid, cid))
    for aid in attachments:
        c.execute("UPDATE attachments SET message_id=? WHERE id=? AND message_id IS NULL "
                  "AND tool_call_id IS NULL AND state='ready'", (mid, aid))
    m = {"id": mid, "parent_id": parent, "role": role, "content": text,
         "ordinal": ordinal, "turn_id": turn_id, "author": author, "created_at": ts,
         "attachments": files.of_message(c, mid),
         "intercom": view(c, intercom_id, cid) if intercom_id is not None else None,
         "meta": meta_view(meta, role, quiet)}
    store.event(c, cid, MESSAGE_CREATED, {
        "message_id": mid, "parent_id": parent, "role": role, "content": text,
        "ordinal": ordinal, "author": author, "attachments": m["attachments"],
        "intercom": m["intercom"], "meta": m["meta"], "turn_id": turn_id})
    _say_if_new_version(c, store, cid, parent, mid, old_leaf)
    return m


def path(c: sqlite3.Connection, cid: str) -> list[dict]:
    """The active path, root first: parent pointers walked up from the active leaf."""
    row = c.execute("SELECT active_leaf_id FROM conversations WHERE id=?",
                    (cid,)).fetchone()
    if row is None:
        raise KeyError(cid)
    chain: list[dict] = []
    mid = row["active_leaf_id"]
    seen: set[str] = set()
    while mid and mid not in seen:
        seen.add(mid)
        m = c.execute(
            "SELECT m.id, m.parent_id, m.role, m.content, m.reasoning, m.ordinal, "
            "m.turn_id, m.author, m.intercom_id, m.created_at, m.meta, "
            "m.finish_reason, t.quiet "
            "FROM messages m LEFT JOIN turns t ON t.id = m.turn_id WHERE m.id=?",
            (mid,)).fetchone()
        if m is None:
            break
        chain.append(dict(m))
        mid = m["parent_id"]
    chain.reverse()
    for m in chain:
        m["meta"] = meta_view(m["meta"], m["role"], m.pop("quiet"))
        m["siblings"] = int(c.execute(
            f"SELECT count(*) FROM messages WHERE parent_id IS ? AND conversation_id = ? "
            f"AND {_VERSION}", (m["parent_id"], cid, m["id"])).fetchone()[0])
        m["attachments"] = files.of_message(c, m["id"])
        iid = m.pop("intercom_id")
        m["intercom"] = view(c, iid, cid) if iid is not None else None
    return chain


# A version is a message that holds something. A reply whose request failed before the
# model said anything (no text, no thinking, no tool call under it) is kept with its raw
# error, but is no version of its own: a Retry after it is the same attempt again. The
# message asked about (the ? parameter) always counts, so it has its place in the list.
_VERSION = ("(role != 'assistant' OR trim(coalesce(content, '')) != '' "
            "OR coalesce(reasoning, '') != '' OR id = ? OR EXISTS (SELECT 1 FROM messages "
            "ch WHERE ch.parent_id = messages.id))")


def turn_tail(c: sqlite3.Connection, turn_id: str, root: str | None,
              leaf_id: str | None) -> list[dict] | None:
    """The turn's own messages from the active leaf up to its root message, leaf
    first. [] when the leaf is the root (nothing written yet). None when the leaf is
    not in this turn (someone wrote, edited or switched version since)."""
    if root is None:
        return None
    out: list[dict] = []
    mid = leaf_id
    seen: set[str] = set()
    while mid and mid not in seen:
        if mid == root:
            return out
        seen.add(mid)
        m = c.execute("SELECT id, parent_id, role, content, reasoning, turn_id, "
                      "finish_reason FROM messages WHERE id=?", (mid,)).fetchone()
        if m is None:
            return None
        # a message the turn took at a step boundary carries its id (intercom/deliver.py)
        if m["role"] in ("user", "assistant") and m["turn_id"] != turn_id:
            return None
        out.append(dict(m))
        mid = m["parent_id"]
    return None


def _unresolved(c: sqlite3.Connection, message_id: str) -> list[str]:
    """Tool calls that have not settled, on this message OR anywhere below it."""
    return [r["name"] for r in c.execute(
        "WITH RECURSIVE sub(id) AS (SELECT ? UNION ALL "
        "  SELECT m.id FROM messages m JOIN sub ON m.parent_id = sub.id) "
        "SELECT tc.name FROM tool_calls tc JOIN sub ON tc.message_id = sub.id "
        "WHERE tc.state IN ('pending','running','detached')", (message_id,))]


def _deepest_leaf(c: sqlite3.Connection, message_id: str) -> str:
    """Descend through preferred_child_id to the end of the path."""
    mid = message_id
    seen: set[str] = set()
    while mid not in seen:
        seen.add(mid)
        row = c.execute(
            "SELECT m.preferred_child_id FROM messages m JOIN messages k ON "
            "k.id = m.preferred_child_id AND k.parent_id = m.id WHERE m.id=?",
            (mid,)).fetchone()          # a preferred child moved under another is no child
        nxt = row["preferred_child_id"] if row else None
        if not nxt:
            row = c.execute(
                "SELECT id FROM messages WHERE parent_id=? "
                "ORDER BY ordinal DESC, created_at DESC LIMIT 1", (mid,)).fetchone()
            nxt = row["id"] if row else None
        if not nxt:
            return mid
        mid = nxt
    return mid


def live_queue(c: sqlite3.Connection, cid: str) -> list[dict]:
    """The conversation's live turns in the order they run: [{turn_id, quiet, state}]."""
    return [{"turn_id": r["id"], "quiet": bool(r["quiet"]), "state": r["state"]}
            for r in c.execute(
        "SELECT t.id, t.quiet, t.state FROM turns t WHERE "
        f"t.conversation_id=? AND t.state IN {LIVE_SQL} ORDER BY t.state = 'queued', "
        f"{QUEUE_ORDER}", (cid,))]


def turn_state(c: sqlite3.Connection, store, cid: str, turn_id: str, state: str,
               **more) -> int:
    """The one `turn.state` event: the turn, its state, the conversation's live queue
    after the change (`queue`, live_queue) and what `more` adds. -> its seq."""
    return store.event(c, cid, TURN_STATE, {"turn_id": turn_id, "state": state,
                                              "queue": live_queue(c, cid), **more})


def queue_turn(c: sqlite3.Connection, store, cid: str, root: str | None, *,
               origin: str = "user", again: str | None = None) -> tuple[str, int]:
    """A queued turn answering `root`; None: the active leaf when a worker takes it
    (turns/store.py claim), so turns queued together answer one after the other.
    `again` is the message it writes a new version of (an answer again: text only).
    -> (turn id, seq). ValueError: a stopped mission's conversation, where nothing runs
    again."""
    if c.execute("SELECT 1 FROM missions WHERE conversation_id=? AND status='stopped'",
                 (cid,)).fetchone():
        raise ValueError("The mission is stopped. Start a new one.")
    if origin == "user":
        from ..plans.store import answered_sync
        answered_sync(c, store, cid)
    tid = new_id()
    c.execute("INSERT INTO turns(id,conversation_id,root_message_id,state,origin,again,"
              "created_at) VALUES(?,?,?,'queued',?,?,?)",
              (tid, cid, root, origin, again, now()))
    return tid, turn_state(c, store, cid, tid, "queued")


def again_as(c: sqlite3.Connection, turn_id: str | None, message_id: str | None) -> dict:
    """What a turn that answers again (a retry, a regenerate, a continue) in place of
    message `message_id` takes: queue_turn's keywords."""
    return {"again": message_id}


def _insert_under(c: sqlite3.Connection, cid: str, parent: str, role: str, text: str,
                  meta: dict) -> str:
    """A plain row under `parent` (no event: the caller says the path changed)."""
    mid = new_id()
    ordinal = c.execute("SELECT coalesce(max(ordinal), -1) + 1 FROM messages WHERE "
                        "parent_id=?", (parent,)).fetchone()[0]
    c.execute("INSERT INTO messages(id,conversation_id,parent_id,role,content,ordinal,author,"
              "created_at,meta) VALUES(?,?,?,?,?,?,'model',?,?)",
              (mid, cid, parent, role, text, ordinal, now(),
               json.dumps(meta, ensure_ascii=False)))
    c.execute("UPDATE messages SET preferred_child_id=? WHERE id=?", (mid, parent))
    return mid


# ---- the runner's reply ---------------------------------------------------------------

async def start_assistant_message(store, cid: str, parent_id: str | None,
                                  turn_id: str) -> str:
    """A new step of the turn's reply."""
    mid = new_id()

    def fn(c: sqlite3.Connection) -> str:
        old_leaf = leaf(c, cid)
        ordinal = c.execute(
            "SELECT coalesce(max(ordinal), -1) + 1 FROM messages WHERE parent_id IS ? "
            "AND conversation_id = ?", (parent_id, cid)).fetchone()[0]
        t = c.execute("SELECT quiet FROM turns WHERE id=?", (turn_id,)).fetchone()
        c.execute(
            "INSERT INTO messages(id,conversation_id,parent_id,role,ordinal,"
            "turn_id,author,created_at) VALUES(?,?,?,'assistant',?,?,'model',?)",
            (mid, cid, parent_id, ordinal, turn_id, now()))
        if parent_id:
            c.execute("UPDATE messages SET preferred_child_id=? WHERE id=?",
                      (mid, parent_id))
        c.execute("UPDATE conversations SET active_leaf_id=? WHERE id=?", (mid, cid))
        store.event(c, cid, MESSAGE_CREATED,
                    {"message_id": mid, "parent_id": parent_id, "role": "assistant",
                     "ordinal": ordinal, "turn_id": turn_id,
                     "meta": meta_view(None, "assistant", t and t["quiet"])})
        _say_if_new_version(c, store, cid, parent_id, mid, old_leaf)
        return mid

    return await store.write(fn)


async def append_delta(store, cid: str, message_id: str, text: str, content: str,
                       quiet: bool = False) -> int:
    """New text of a streaming reply, and its whole text so far onto the row in the
    same write: the row is never behind the log. The event is text only, no offsets:
    Python counts codepoints, JS counts UTF-16 units. A quiet turn's text is kept on
    the row and never streamed (no event, 0)."""
    def fn(c: sqlite3.Connection) -> int:
        c.execute("UPDATE messages SET content=? WHERE id=?", (mask(content), message_id))
        if quiet:
            return 0
        return store.event(c, cid, MESSAGE_DELTA, {"message_id": message_id, "text": text})

    return await store.write(fn)


async def finish_assistant_message(store, cid: str, message_id: str, text: str,
                                   reasoning: str | None = None,
                                   finish_reason: str | None = None,
                                   raw: dict | None = None, rewrite: bool = False,
                                   thinking: list | None = None) -> int:
    """The reply's final text, as `message.done` when its stream ended, or as
    `message.rewritten` (`rewrite`) when the text changes after that (citations
    checked, links made). `finish_reason` is how the model's stream ended ("stop",
    "length", ...); without one the stored reason stays (a rewrite, or a cut stream,
    which has none since it started). `raw` is what the server answered
    (raw_response). `thinking`: the reply's signed thinking blocks (Done.thinking),
    kept in its meta to go back with it (turns/compactions.context_view). The agent's text is
    stored without a known secret in it (errors.mask): a key, a vault password; when one
    was in it, the pieces it streamed in leave the stored events too."""
    streamed = (text, reasoning or "")
    text, reasoning = mask(text), mask(reasoning or "") or None

    def fn(c: sqlite3.Connection) -> int:
        if streamed != (text, reasoning or ""):
            c.execute("UPDATE events SET data=json_set(data, '$.text', '') WHERE "
                      "conversation_id=? AND type IN ('message.delta', 'message.reasoning') "
                      "AND json_extract(data, '$.message_id')=?", (cid, message_id))
        c.execute("UPDATE messages SET content=?, reasoning=?, "
                  "finish_reason=coalesce(?, finish_reason) WHERE id=?",
                  (text, reasoning, finish_reason, message_id))
        if thinking:
            c.execute("UPDATE messages SET meta=json_set(coalesce(meta, '{}'), "
                      "'$.thinking', json(?)) WHERE id=?",
                      (json.dumps(thinking, ensure_ascii=False), message_id))
        if raw is not None:
            c.execute("INSERT OR REPLACE INTO raw_responses(message_id, body) VALUES(?,?)",
                      (message_id, json.dumps(mask_all(raw), ensure_ascii=False)))
        return store.event(c, cid, MESSAGE_REWRITTEN if rewrite else MESSAGE_DONE,
                           {"message_id": message_id, "text": text,
                            "has_reasoning": bool(reasoning)})

    return await store.write(fn)


async def raw_response(store, cid: str, message_id: str) -> dict | None:
    """What the server answered for this reply, None when nothing was kept. KeyError:
    no such message here."""
    row = await store.read(lambda c: c.execute(
        "SELECT r.body FROM messages m LEFT JOIN raw_responses r ON r.message_id = m.id "
        "WHERE m.id=? AND m.conversation_id=?", (message_id, cid)).fetchone())
    if row is None:
        raise KeyError(message_id)
    return json.loads(row["body"]) if row["body"] else None


async def reopen_message(store, cid: str, message_id: str) -> int:
    """Continue streams into an existing reply: the client moves its cursor back. The
    reply is open again, so it has no finish reason until its stream ends."""
    def fn(c: sqlite3.Connection) -> int:
        c.execute("UPDATE messages SET finish_reason=NULL WHERE id=?", (message_id,))
        return store.event(c, cid, MESSAGE_REOPENED, {"message_id": message_id})

    return await store.write(fn)


async def drop_empty(store, cid: str, message_id: str) -> bool:
    """A reply that holds nothing (no text, no thinking, no call, nothing under it)
    removed, its parent the leaf again where it was the leaf; `message.purged` says so.
    -> whether it went."""
    def fn(c: sqlite3.Connection) -> bool:
        m = c.execute("SELECT parent_id FROM messages WHERE id=? AND conversation_id=? AND "
                      "role='assistant' AND coalesce(content, '')='' AND "
                      "coalesce(reasoning, '')=''", (message_id, cid)).fetchone()
        if m is None or c.execute(
                "SELECT 1 FROM messages WHERE parent_id=? UNION ALL SELECT 1 FROM tool_calls "
                "WHERE message_id=?", (message_id, message_id)).fetchone():
            return False
        parent = m["parent_id"]
        if parent is not None:
            c.execute("UPDATE messages SET preferred_child_id=(SELECT id FROM messages WHERE "
                      "parent_id=? AND id != ? ORDER BY ordinal DESC LIMIT 1) WHERE id=? AND "
                      "preferred_child_id=?", (parent, message_id, parent, message_id))
        here = leaf(c, cid)
        if here == message_id:
            here = parent
            c.execute("UPDATE conversations SET active_leaf_id=? WHERE id=?", (here, cid))
        c.execute("DELETE FROM requests WHERE message_id=?", (message_id,))
        c.execute("DELETE FROM raw_responses WHERE message_id=?", (message_id,))
        c.execute("DELETE FROM messages WHERE id=?", (message_id,))
        store.event(c, cid, MESSAGE_PURGED, {"message_ids": [message_id],
                                               "active_leaf_id": here})
        return True
    return await store.write(fn)


async def active_leaf(store, cid: str) -> str | None:
    return await store.read(lambda c: leaf(c, cid))


# ---- the user's tree edits --------------------------------------------------------------

async def regenerate(store, cid: str) -> tuple[str, int]:
    """A new turn for the last user message when no reply followed it."""
    def fn(c: sqlite3.Connection) -> tuple[str, int]:
        row = c.execute("SELECT active_leaf_id FROM conversations WHERE id=?",
                        (cid,)).fetchone()
        if row is None:
            raise KeyError(cid)
        refuse_if_live(c, cid)
        # walk up from the leaf: a reply that never said anything (tool calls only, or
        # a failed stream) does not count as an answer
        mid = row["active_leaf_id"]
        tried = None             # the turn that tried to answer: who speaks again
        while True:
            m = c.execute("SELECT id, role, parent_id, content, turn_id FROM messages "
                          "WHERE id=?", (mid,)).fetchone()
            if m is None:
                raise ValueError("the last message is not yours")
            if m["role"] == "user":
                break
            if m["role"] == "assistant" and (m["content"] or "").strip():
                failed = bool(m["turn_id"] and (c.execute(
                    "SELECT 1 FROM turns WHERE id=? AND state='failed'",
                    (m["turn_id"],)).fetchone()))
                if not failed:
                    raise ValueError("the last message is not yours")
            tried = tried or (m["turn_id"] if m["role"] == "assistant" else None)
            mid = m["parent_id"]
        # one that failed before it wrote anything
        tried = tried or (c.execute(
            "SELECT id FROM turns WHERE conversation_id=? AND root_message_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (cid, mid)).fetchone() or [None])[0]
        if mid != row["active_leaf_id"]:
            c.execute("UPDATE conversations SET active_leaf_id=? WHERE id=?", (mid, cid))
            c.execute("UPDATE messages SET preferred_child_id=NULL WHERE id=?", (mid,))
            store.event(c, cid, VERSION_CHANGED, {"parent_id": mid, "active_leaf_id": mid})
        return queue_turn(c, store, cid, mid, **again_as(c, tried, mid))

    return await store.write(fn)


async def retry(store, cid: str, message_id: str) -> tuple[str, int]:
    """A new version of this node, beside it, and generate into it."""
    def fn(c: sqlite3.Connection) -> tuple[str, int]:
        msg = c.execute("SELECT role, parent_id, turn_id FROM messages WHERE id=? AND "
                        "conversation_id=?", (message_id, cid)).fetchone()
        if msg is None:
            raise KeyError(message_id)
        refuse_if_live(c, cid)
        if stuck := _unresolved(c, message_id):
            raise Unresolved(stuck)
        parent = msg["parent_id"]
        if parent is None:
            raise ValueError("Nothing to retry. This is the first message.")
        c.execute("UPDATE conversations SET active_leaf_id=? WHERE id=?", (parent, cid))
        c.execute("UPDATE messages SET preferred_child_id=NULL WHERE id=?", (parent,))
        out = queue_turn(c, store, cid, parent, **again_as(c, msg["turn_id"], message_id))
        store.event(c, cid, VERSION_CHANGED, {"parent_id": parent, "active_leaf_id": parent})
        return out

    return await store.write(fn)


async def edit(store, cid: str, message_id: str,
               text: str) -> tuple[str, str | None, int]:
    """A new version (sibling) of a message carrying new text, which becomes the active
    leaf; what followed the old one stays on its path. A user message is answered (a queued turn) when nothing
    followed it; its own is not: its edited reply keeps the old one's thinking, its turn and
    the steps above it (its tool calls), only its text is new.
    -> (message id, turn id or None, seq)."""
    def fn(c: sqlite3.Connection) -> tuple[str, str | None, int]:
        msg = c.execute("SELECT role, parent_id, reasoning, turn_id, finish_reason, meta "
                        "FROM messages WHERE id=? AND conversation_id=?",
                        (message_id, cid)).fetchone()
        if msg is None:
            raise KeyError(message_id)
        if msg["role"] not in ("user", "assistant"):
            raise ValueError("Only your messages and {{name}}'s replies can be edited.")
        refuse_if_live(c, cid)
        if msg["role"] == "assistant":
            m = append(c, store, cid, msg["parent_id"], "assistant", text, "model",
                       turn_id=msg["turn_id"], meta=unsigned(msg["meta"]))
            c.execute("UPDATE messages SET reasoning=?, finish_reason=? WHERE id=?",
                      (msg["reasoning"], msg["finish_reason"], m["id"]))
            seq = c.execute("SELECT next_seq - 1 FROM conversations WHERE id=?",
                            (cid,)).fetchone()[0]
            return m["id"], None, int(seq)
        if stuck := _unresolved(c, message_id):
            raise Unresolved(stuck)
        # a new intercom row like the one edited, with the new text, delivered at once
        from ..intercom.deliver import land
        old = c.execute("SELECT m.parent_id, i.from_addr, i.kind, i.data FROM messages m "
                        "LEFT JOIN intercom i ON i.id = m.intercom_id WHERE m.id=?",
                        (message_id,)).fetchone()
        data = {k: v for k, v in json.loads(old["data"] or "{}").items()
                if k not in ("attachments", "message_id")}
        iid = intercom_insert(c, old["from_addr"] or "user", f"agent:{cid}",
                              old["kind"] or "message", text, data=data)
        row = c.execute("SELECT * FROM intercom WHERE id=?", (iid,)).fetchone()
        mid = land(c, store, row, cid, old["parent_id"], None)["id"]
        tid, seq = queue_turn(c, store, cid, mid)
        c.execute("UPDATE intercom_deliveries SET turn_id=? WHERE intercom_id=?", (tid, iid))
        return mid, tid, seq

    return await store.write(fn)


KEEP_TIME = "keep_time:"    # + a turn id: the conversation's time to put back after it


async def continue_message(store, cid: str, message_id: str) -> tuple[str, str, int]:
    """The agent's finished reply at the active leaf, carried on: a new version of it holding
    its text, and a queued turn that streams on into that version (the runner's
    reopen step: its text as a prefilled assistant turn, thinking off). The old text
    stays as the previous version. -> (message id, turn id, seq). ValueError with the
    sentence why when it cannot be continued."""
    def fn(c: sqlite3.Connection) -> tuple[str, str, int]:
        m = c.execute("SELECT role, parent_id, content, created_at, turn_id, meta "
                      "FROM messages WHERE id=? AND conversation_id=?",
                      (message_id, cid)).fetchone()
        if m is None:
            raise KeyError(message_id)
        if m["role"] != "assistant" or m["parent_id"] is None:
            raise ValueError("Only {{name}}'s replies can be continued.")
        try:
            refuse_if_live(c, cid)
        except Busy:
            raise ValueError("{{name}} is still answering.") from None
        if leaf(c, cid) != message_id:
            raise ValueError("Only the last message can be continued.")
        if not (m["content"] or "").strip():
            raise ValueError("The reply has no text to continue.")
        # the new version keeps the reply's time, and the conversation its place in the
        # list: its time now is put back when the turn ends (turns/store.py set_state)
        was = c.execute("SELECT updated_at FROM conversations WHERE id=?", (cid,)).fetchone()[0]
        tid, seq = queue_turn(c, store, cid, m["parent_id"],
                              **again_as(c, m["turn_id"], message_id))
        c.execute("INSERT INTO kv(k, v) VALUES(?, ?)", (KEEP_TIME + tid, was))
        new = append(c, store, cid, m["parent_id"], "assistant", m["content"], "model",
                     turn_id=tid, meta=unsigned(m["meta"]))
        c.execute("UPDATE messages SET created_at=? WHERE id=?", (m["created_at"], new["id"]))
        return new["id"], tid, seq

    return await store.write(fn)


async def switch_version(store, cid: str, message_id: str) -> int:
    """Point the active path at this sibling. Never runs a turn."""
    def fn(c: sqlite3.Connection) -> int:
        msg = c.execute("SELECT parent_id FROM messages WHERE id=? AND "
                        "conversation_id=?", (message_id, cid)).fetchone()
        if msg is None:
            raise KeyError(message_id)
        refuse_if_live(c, cid)
        if msg["parent_id"]:
            c.execute("UPDATE messages SET preferred_child_id=? WHERE id=?",
                      (message_id, msg["parent_id"]))
        end = _deepest_leaf(c, message_id)
        c.execute("UPDATE conversations SET active_leaf_id=? WHERE id=?", (end, cid))
        return store.event(c, cid, VERSION_CHANGED,
                           {"parent_id": msg["parent_id"], "preferred_child_id": message_id,
                            "active_leaf_id": end})

    return await store.write(fn)


async def siblings(store, cid: str, message_id: str) -> dict:
    def fn(c: sqlite3.Connection) -> dict:
        msg = c.execute("SELECT parent_id FROM messages WHERE id=? AND "
                        "conversation_id=?", (message_id, cid)).fetchone()
        if msg is None:
            raise KeyError(message_id)
        rows = c.execute(
            f"SELECT id, role, content, ordinal, created_at FROM messages "
            f"WHERE parent_id IS ? AND conversation_id = ? AND {_VERSION} "
            f"ORDER BY ordinal, created_at", (msg["parent_id"], cid, message_id)).fetchall()
        ids = [r["id"] for r in rows]
        return {"parent_id": msg["parent_id"], "siblings": [dict(r) for r in rows],
                "index": ids.index(message_id), "count": len(ids)}

    return await store.read(fn)


async def branch(store, cid: str, message_id: str) -> dict | None:
    """Branch from here: a new chat with the same settings holding every
    message of the path up to and including `message_id`, yours and its own (tool rows left
    out; its own in a row joined into one, so each turn is one reply),
    with your files. Named "Branch of <title>". -> {id, title}, None when the message is
    not in that conversation."""
    def fn(c: sqlite3.Connection) -> dict | None:
        src = c.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        rows, mid = [], message_id
        while mid:
            m = c.execute("SELECT * FROM messages WHERE id=? AND conversation_id=?",
                          (mid, cid)).fetchone()
            if m is None:
                return None
            rows.append(m)
            mid = m["parent_id"]
        if src is None or not rows:
            return None
        kept: list[dict] = []
        for m in reversed(rows):
            if m["role"] not in ("user", "assistant"):
                continue
            att = [a["id"] for a in files.of_message(c, m["id"])] if m["role"] == "user" else []
            if kept and kept[-1]["role"] == m["role"] == "assistant":
                if m["content"].strip():
                    kept[-1]["content"] = f"{kept[-1]['content'].rstrip()}\n\n{m['content']}"
                continue
            if m["role"] == "assistant" and not m["content"].strip():
                continue
            kept.append({"role": m["role"], "content": m["content"], "author": m["author"],
                         "meta": unsigned(m["meta"]), "created_at": m["created_at"],
                         "files": att})
        new, ts = new_id(), now()
        title = f"Branch of {src['title']}" if src["title"] else "Branch"
        c.execute(
            "INSERT INTO conversations(id, title, kind, model_id, thinking, notes_off, "
            "auto_context, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (new, title[:200], "chat", src["model_id"], src["thinking"],
             src["notes_off"], src["auto_context"], ts, ts))
        parent = None
        for m in kept:
            mid = new_id()
            c.execute("INSERT INTO messages(id,conversation_id,parent_id,role,content,author,"
                      "created_at,meta) VALUES(?,?,?,?,?,?,?,?)",
                      (mid, new, parent, m["role"], m["content"], m["author"],
                       m["created_at"], m["meta"]))
            if parent:
                c.execute("UPDATE messages SET preferred_child_id=? WHERE id=?", (mid, parent))
            for aid in m["files"]:
                c.execute("INSERT INTO attachments(id, message_id, sha256, filename, kind, "
                          "state, created_at, description, described_by, described_for) "
                          "SELECT ?, ?, sha256, filename, kind, state, created_at, "
                          "description, described_by, described_for FROM attachments "
                          "WHERE id=?", (new_id(), mid, aid))
            parent = mid
        c.execute("UPDATE conversations SET active_leaf_id=? WHERE id=?", (parent, new))
        from .store import announce
        announce(store, c, new)
        return {"id": new, "title": title}

    return await store.write(fn)


def final_words(c: sqlite3.Connection, turn_id: str) -> str:
    """The agent's answer of a turn: its last reply that called no tool, without web citation
    marks (their numbers mean something only in its context). "" when it has none:
    what it said between tool calls is no answer."""
    from ..skills.web import uncite
    r = c.execute("SELECT content FROM messages m WHERE turn_id = ? AND role = 'assistant' "
                  "AND coalesce(content, '') <> '' AND NOT EXISTS (SELECT 1 FROM tool_calls "
                  "tc WHERE tc.message_id = m.id) ORDER BY created_at DESC, rowid DESC "
                  "LIMIT 1", (turn_id,)).fetchone()
    return uncite(r[0]).strip() if r else ""
