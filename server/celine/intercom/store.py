"""The intercom's rows: one per message, one delivery per recipient. Rows are never
deleted; a cancel marks one cancelled."""
from __future__ import annotations

import json
import sqlite3

from ..core.ids import now
from .. import homes
from .addresses import resolve

# kv: what it reads before its first message in a chat start_chat made, by its id
# (intercom/skill.py opening, turns/context.py)
OPENING = "chat_opening:"


def insert(c: sqlite3.Connection, from_addr: str, to: str, kind: str, text: str, *,
           subject: str | None = None, data: dict | None = None, wake: bool = True,
           due_at: str | None = None, until_at: str | None = None,
           reply_to: int | None = None, created_at: str | None = None,
           store=None, turn: str | None = None) -> int:
    """One message and a pending delivery per recipient, inside a write. A reply joins
    the thread of the message it answers and keeps its subject; without `to` it goes to
    the sender of that message. `turn`: the sender's turn; without reply_to the message
    answers in a thread anyway (follows).
    -> its id."""
    thread = None
    if reply_to is not None:
        r = c.execute("SELECT thread, subject, from_addr FROM intercom WHERE id=?",
                      (reply_to,)).fetchone()
        if r is None:
            raise KeyError(reply_to)
        thread, subject = r["thread"], subject or r["subject"]
        if not to.strip():
            to = r["from_addr"]       # a host script's: resolve makes it the user
    stored, targets = resolve(c, to, from_addr, store)
    if reply_to is None and turn is not None:
        reply_to = follows(c, from_addr, stored, turn)
        if reply_to is not None:
            r = c.execute("SELECT thread, subject FROM intercom WHERE id=?",
                          (reply_to,)).fetchone()
            thread, subject = r["thread"], subject or r["subject"]
    iid = c.execute(
        "INSERT INTO intercom(created_at, from_addr, to_addr, kind, subject, text, data, "
        "wake, due_at, until_at, reply_to, thread) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
        "RETURNING id",
        (created_at or now(), from_addr, stored, kind, subject,
         text, json.dumps(data or {}, ensure_ascii=False), int(bool(wake)), due_at,
         until_at, reply_to, thread)).fetchone()[0]
    if thread is None:
        c.execute("UPDATE intercom SET thread=id WHERE id=?", (iid,))
    for addr, cid in targets:
        c.execute("INSERT INTO intercom_deliveries(intercom_id, to_addr, conversation_id, "
                  "state) VALUES(?,?,?,'pending')", (iid, addr, cid))
    to_user = "user" in (a for a, _ in targets)
    if store is not None and from_addr.startswith("agent:") and to_user:
        from ..events import added
        added(store, c, "message", from_addr[6:])     # its own to the user: Events
    # one that waits for the user now (a job result is no thread; a due time ahead
    # lands later): a notice
    if store is not None and to_user and from_addr != "user" and kind != "job" and \
            not (due_at and due_at > now()):
        notice_user(c, store, c.execute("SELECT * FROM intercom WHERE id=?", (iid,)).fetchone())
    return iid


def notice_user(c: sqlite3.Connection, store, row: sqlite3.Row) -> None:
    """Inside a write: message `row` waits for the user now, as a notice (events.notice)."""
    from ..events import notice
    notice(store, c, "intercom", name_of(c, row["from_addr"]), row["subject"] or row["text"],
           f"/intercom/{row['thread']}", f"intercom:{row['thread']}")


def answering(c: sqlite3.Connection, cid: str, turn: str) -> sqlite3.Row | None:
    """The newest message in a thread that turn `turn` of `cid` took in (no plain chat
    message, no job result): the one it answers."""
    return c.execute(
        "SELECT i.* FROM intercom i JOIN intercom_deliveries d ON d.intercom_id = i.id "
        f"WHERE d.turn_id = ? AND d.conversation_id = ? AND NOT {PLAIN} AND "
        "i.kind <> 'job' AND i.from_addr <> ? ORDER BY i.id DESC LIMIT 1",
        (turn, cid, f"agent:{cid}")).fetchone()


def follows(c: sqlite3.Connection, sender: str, to: str, turn: str) -> int | None:
    """The message a message from `sender` to `to` (stored) answers without a reply_to:
    the one its turn took in from `to`. None: it starts a thread."""
    if sender.startswith("agent:"):
        r = answering(c, sender[6:], turn)
        if r is not None and r["from_addr"] in to.split(", "):
            return r["id"]
    return None


# the names people read for the user and for it; the settings set them when they load
# (settings.compile_her)
NAMES: dict = {"user": "you", "her": ""}
# the app's own senders by name: the app, and Bastet, the guard (guard/bastet.py)
SYSTEM_NAMES = {"app": "the app", "guard": "Bastet"}


def name_of(c: sqlite3.Connection, addr: str) -> str | None:
    """An address as people read it: the user's name; a conversation's mission or
    sub-agent name, else its own name (a team by its first conversation's); a token's
    name; None for an address that names nobody (`all`)."""
    kind, _, rest = addr.partition(":")
    if addr == "user":
        return NAMES["user"]
    if kind == "system":
        return SYSTEM_NAMES.get(rest, rest)
    if kind not in ("agent", "team"):
        return None
    r = c.execute("SELECT coalesce(mi.name, c.agent_name) AS name "
                  "FROM conversations c LEFT JOIN missions mi ON mi.conversation_id = c.id "
                  "WHERE c.id=?", (rest,)).fetchone()
    return None if r is None else r["name"] or NAMES["her"]


def recipients(c: sqlite3.Connection) -> list[dict]:
    """Whom the user can write to, each once: everyone working, every mission that is
    not stopped, then its chats and tasks newest first (nothing archived),
    each followed by its sub-agents. Those after the missions carry `line`, how a
    second step lists them: the conversation's title, a sub-agent as "<name>, in
    <where>"."""
    out: list[dict] = [{"to": "all", "name": "Everyone working", "line": None}]
    tops = c.execute(
        "SELECT c.id, c.title, mi.name AS mission FROM conversations c "
        "LEFT JOIN missions "
        "mi ON mi.conversation_id = c.id WHERE c.agent_of IS NULL AND c.trashed_at IS NULL "
        "AND c.archived_at IS NULL AND coalesce(mi.status, '') <> 'stopped' "
        "ORDER BY mi.name IS NULL, c.updated_at DESC, c.id DESC").fetchall()
    for t in tops:
        where = t["mission"] or t["title"] or "Untitled"
        out.append({"to": f"agent:{t['id']}", "name": t["mission"] or NAMES["her"],
                    "line": None if t["mission"] else where})
        out += [{"to": f"agent:{s['id']}", "name": s["agent_name"],
                 "line": f"{s['agent_name']}, in {where}"}
                for s in c.execute("SELECT id, agent_name FROM conversations WHERE "
                                   "agent_of=? ORDER BY created_at", (t["id"],))]
    return out


def source_of(c: sqlite3.Connection, addr: str) -> dict | None:
    """Where a message from `addr` came from, as pages show it: {kind, title, link (a
    path in the app, homes.of for a conversation, or None)}. kind: chat, mission, run
    (a schedule's run), task (a background task), sub-agent, token (a
    host script's named token), connector (another app sender, like email), app. None for the user and
    for everyone working."""
    kind, _, rest = addr.partition(":")
    if kind == "system":
        if rest == "app":
            return {"kind": "app", "title": None, "link": None}
        if rest == "guard":
            return {"kind": "app", "title": SYSTEM_NAMES["guard"], "link": None}
        if rest == "schedules":
            return {"kind": "run", "title": None, "link": "/routines"}
        if c.execute("SELECT 1 FROM intercom_tokens WHERE name=?", (rest,)).fetchone():
            return {"kind": "token", "title": rest, "link": "/settings/security"}
        return {"kind": "connector", "title": rest, "link": None}
    if kind not in ("agent", "team"):
        return None
    r = c.execute("SELECT c.kind, c.title, c.agent_of, c.agent_name, c.trigger_key, "
                  "mi.name AS mission FROM conversations c LEFT JOIN missions mi "
                  "ON mi.conversation_id = c.id WHERE c.id=?", (rest,)).fetchone()
    if r is None:
        return None
    title, link = r["title"] or "Untitled", homes.of(c, rest)["link"]
    if r["agent_of"]:
        return {"kind": "sub-agent", "title": r["agent_name"], "link": link}
    if r["kind"] == "mission":
        return {"kind": "mission", "title": r["mission"] or title, "link": link}
    if r["kind"] == "background":
        key = r["trigger_key"] or ""
        run = c.execute("SELECT label FROM runs WHERE id=?",
                        (key.split(":")[1],)).fetchone() if key.startswith("run:") else None
        return ({"kind": "run", "title": run["label"], "link": link} if run is not None
                else {"kind": "task", "title": title, "link": link})
    return {"kind": "chat", "title": title, "link": link}


def thread_source(c: sqlite3.Connection, tid: int) -> dict | None:
    """Where thread `tid` comes from: the first one in it who is not the user (one of
    its conversations before the app's senders)."""
    seen = []
    for r in c.execute("SELECT from_addr, to_addr FROM intercom WHERE thread=? ORDER BY id",
                       (tid,)):
        seen += [a for a in (r["from_addr"], *r["to_addr"].split(", ")) if a not in seen]
    for a in sorted(seen, key=lambda a: a.startswith("system:")):
        if (src := source_of(c, a)) is not None:
            return src
    return None


def view(c: sqlite3.Connection, iid: int, cid: str | None = None) -> dict | None:
    """One message as clients and the context builder read it. `mine`: sent by the
    conversation `cid` itself. `source`: where it came from (source_of). `wake`: it
    starts a turn where it lands (a notice with wake off lands quietly)."""
    r = c.execute("SELECT * FROM intercom WHERE id=?", (iid,)).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "kind": r["kind"], "from": r["from_addr"], "to": r["to_addr"],
            "subject": r["subject"], "created_at": r["created_at"], "due_at": r["due_at"],
            "until_at": r["until_at"], "wake": bool(r["wake"]),
            "reply_to": r["reply_to"], "thread": r["thread"],
            "data": json.loads(r["data"] or "{}"), "from_name": name_of(c, r["from_addr"]),
            # each conversation it went to by name (a team by its first conversation's)
            "to_names": {a: name_of(c, a) for a in r["to_addr"].split(", ")},
            "mine": cid is not None and r["from_addr"] == f"agent:{cid}",
            "source": source_of(c, r["from_addr"])}


def state(c: sqlite3.Connection, r: sqlite3.Row) -> str:
    """cancelled, pending (its time ahead, or waiting for a conversation) or sent."""
    if r["cancelled_at"]:
        return "cancelled"
    if (r["due_at"] and r["due_at"] > now()) or c.execute(
            "SELECT 1 FROM intercom_deliveries WHERE intercom_id=? AND state='pending' "
            "AND conversation_id IS NOT NULL", (r["id"],)).fetchone():
        return "pending"
    return "sent"


def listed(c: sqlite3.Connection, to: str | None = None, from_addr: str | None = None,
           scheduled: bool = False, thread: int | None = None, after: int = 0,
           limit: int | None = None) -> list[dict]:
    """Messages to `to` (a stored address) and from `from_addr`, in thread `thread`, with
    an id above `after`, oldest first; with `scheduled` only those with a due time, by
    due time. Each with its `text` and `state`."""
    sql, args = "SELECT * FROM intercom i WHERE i.id > ?", [after]
    if to:
        sql += (" AND EXISTS (SELECT 1 FROM intercom_deliveries d WHERE "
                "d.intercom_id = i.id AND d.to_addr = ?)")
        args.append(to)
    if from_addr:
        sql += " AND i.from_addr = ?"
        args.append(from_addr)
    if thread is not None:
        sql += " AND i.thread = ?"
        args.append(thread)
    if scheduled:
        sql += " AND i.due_at IS NOT NULL ORDER BY i.due_at, i.id"
    else:
        sql += " ORDER BY i.id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [{**view(c, r["id"]), "text": r["text"], "state": state(c, r),
             "unread": _waits(c, r, "user")}
            for r in c.execute(sql, args).fetchall()]


def _waits(c: sqlite3.Connection, r: sqlite3.Row, addr: str) -> bool:
    """Message `r` is due and waits for `addr` (the user: not read yet)."""
    return (not r["due_at"] or r["due_at"] <= now()) and c.execute(
        "SELECT 1 FROM intercom_deliveries WHERE intercom_id=? AND to_addr=? AND "
        "state='pending'", (r["id"], addr)).fetchone() is not None


def read(c: sqlite3.Connection, ids: list[int]) -> list[int]:
    """The user's deliveries of `ids` that landed become read. -> the ids changed."""
    out = []
    for iid in ids:
        if c.execute("UPDATE intercom_deliveries SET state='read', delivered_at=? WHERE "
                     "intercom_id=? AND to_addr='user' AND state='pending' AND EXISTS "
                     "(SELECT 1 FROM intercom i WHERE i.id=? AND (i.due_at IS NULL OR "
                     "i.due_at <= ?))", (now(), iid, iid, now())).rowcount:
            out.append(iid)
    return out


# SQL: 1 when message `i` waits for the address in its first ?: delivered to nobody
# there yet and due by the second ?
_UNREAD = ("EXISTS (SELECT 1 FROM intercom_deliveries d WHERE d.intercom_id = i.id AND "
           "d.to_addr = ? AND d.state = 'pending' AND (i.due_at IS NULL OR i.due_at <= ?))")


KINDS = ("message", "brief", "report", "job", "notice", "trigger")

# The user's own message as typed (the composer): a conversation message
# with no number (render.header), so no thread of its own. A note to themselves is one.
PLAIN = ("(i.from_addr = 'user' AND i.to_addr <> 'user' AND i.kind = 'message' AND "
         "i.due_at IS NULL AND i.until_at IS NULL AND i.reply_to IS NULL)")

# A thread the app opened into a background task (a trigger's fire, a run's prompt and
# its partner's lines, a notice): it stays inside that conversation, never listed.
# What it sends from there in a thread of its own (to the user) is listed.
BACKGROUND = ("EXISTS (SELECT 1 FROM intercom r JOIN intercom_deliveries d ON "
              "d.intercom_id = r.id JOIN conversations bg ON bg.id = d.conversation_id "
              "WHERE r.id = i.thread AND r.from_addr LIKE 'system:%' AND "
              "bg.kind = 'background')")


def _mine(addr: str | None, received: bool) -> tuple[str, list]:
    """SQL and its arguments: message `i` came to `addr`, or (not `received`) `addr`
    wrote it; every message when `addr` is None."""
    if addr is None:
        return "1", []
    to_addr = ("EXISTS (SELECT 1 FROM intercom_deliveries d WHERE d.intercom_id = i.id "
               "AND d.to_addr = ?)")
    if received:
        return to_addr, [addr]
    return f"(i.from_addr = ? OR {to_addr})", [addr, addr]


GUARD = "system:guard"


def threads(c: sqlite3.Connection, addr: str | None, unread: bool = False,
            reader: str | None = None, received: bool = False, kind: str | None = None,
            with_: str | None = None) -> list[dict]:
    """The threads `addr` wrote in or received in (None: every thread; `received`: only
    the messages that came to `addr`, so the count and the newest are of those), newest
    activity first, as {thread, last (its newest id), count, unread (waiting for
    `reader`, default `addr`), with (everyone in it but `reader`, addresses), name (them
    by name)}. Only messages of `kind`; without it, no job results (`jobs` counts them).
    Never the threads of a background task (BACKGROUND: GET /api/background-conversations has them).
    The user's plain chat messages are never listed; cancelled ones are (marked by the
    newest's state). `with_`: only the threads someone is in by that name, address or
    conversation id ("user": the user). The name leaves out the app's senders when
    someone else is in it ("nightly sweep", not "the app, nightly sweep")."""
    ts, reader = now(), reader or addr
    mine, margs = _mine(addr, received)
    where = f"NOT {PLAIN} AND NOT {BACKGROUND} AND {mine}"
    args = [reader, ts, *margs]
    if kind:
        where += " AND i.kind = ?"
        args.append(kind)
    else:
        where += " AND i.kind <> 'job'"
    rows = c.execute(
        f"SELECT i.thread, max(i.id) AS last, count(*) AS n, sum({_UNREAD}) AS unread, "
        "(SELECT group_concat(a, ', ') FROM (SELECT t.from_addr || ', ' || t.to_addr AS a "
        "  FROM intercom t WHERE t.thread = i.thread ORDER BY t.id)) AS addrs "
        f"FROM intercom i WHERE {where} GROUP BY i.thread " +
        ("HAVING unread > 0 " if unread else "") + "ORDER BY last DESC", args).fetchall()
    names: dict[str, str] = {}

    def named(a: str) -> str:
        if a not in names:
            names[a] = name_of(c, a) or "Everyone working"
        return names[a]

    want = with_ or ""
    out = []
    for r in rows:
        others = list(dict.fromkeys(a for a in r["addrs"].split(", ") if a != reader))
        # Bastet's threads have their own tab (with_ GUARD), never Waiting for you
        if unread and GUARD in others:
            continue
        if want and not any(want in (a, a.partition(":")[2], named(a)) for a in others):
            continue
        people = [a for a in others if not a.startswith("system:")] or others
        name = ", ".join(dict.fromkeys(named(a) for a in people)) or \
            name_of(c, reader or "user") or ""
        out.append({"thread": r["thread"], "last": r["last"], "count": r["n"],
                    "unread": r["unread"], "with": others, "name": name})
    return out


def waiting(c: sqlite3.Connection, addr: str) -> int:
    """How many messages wait for `addr` (the user: landed and not read yet): the
    unread of the threads its Waiting list shows, so a badge and the list agree."""
    return sum(t["unread"] for t in threads(c, addr, unread=True, received=True))


def waiting_ids(c: sqlite3.Connection, addr: str) -> list[int]:
    """The messages that wait for `addr` in its Waiting list: what Dismiss all reads."""
    tids = [t["thread"] for t in threads(c, addr, unread=True, received=True)]
    if not tids:
        return []
    return [r[0] for r in c.execute(
        f"SELECT i.id FROM intercom i WHERE i.thread IN ({','.join('?' * len(tids))}) AND "
        f"NOT {PLAIN} AND i.kind <> 'job' AND {_UNREAD}", [*tids, addr, now()])]


def jobs(c: sqlite3.Connection, addr: str | None, received: bool = False) -> dict:
    """The job results among what `addr` wrote or received: {count, newest}."""
    mine, args = _mine(addr, received)
    r = c.execute(f"SELECT count(*), max(i.id) FROM intercom i WHERE i.kind = 'job' AND "
                  f"i.cancelled_at IS NULL AND {mine}", args).fetchone()
    return {"count": r[0], "newest": r[1]}


def thread_view(c: sqlite3.Connection, t: dict) -> dict:
    """A thread of `threads` as lists show it: who it is with by name, the first
    message's subject, where it comes from, the count, how many wait, and the newest
    message with its state."""
    first = c.execute("SELECT subject FROM intercom WHERE id=?", (t["thread"],)).fetchone()
    last = c.execute("SELECT * FROM intercom WHERE id=?", (t["last"],)).fetchone()
    return {"thread": t["thread"], "name": t["name"],
            "subject": first["subject"] if first else None, "count": t["count"],
            "unread": t["unread"], "source": thread_source(c, t["thread"]),
            "last": {**view(c, t["last"]), "text": last["text"], "state": state(c, last)}}


def grouped(c: sqlite3.Connection, found: list[dict], per: int, limit: int,
            before: int | None = None) -> tuple[list[dict], int | None]:
    """The threads of `threads` grouped by who they are with, the group with the newest
    activity first: {name (who by name), addresses, threads (how many), waiting (how many
    of them wait), items (its `per` newest threads, as thread_view), more (the rest)}.
    `limit` groups, those whose newest thread is below `before`. -> (the groups, the
    `before` of the next page or None)."""
    groups: dict[tuple, dict] = {}
    for t in found:
        g = groups.setdefault(tuple(sorted(t["with"])), {
            "name": t["name"], "addresses": t["with"], "threads": 0, "waiting": 0,
            "newest": t["last"], "items": [], "more": 0})
        g["threads"] += 1
        g["waiting"] += 1 if t["unread"] else 0
        if len(g["items"]) < per:
            g["items"].append(t)
        else:
            g["more"] += 1
    rows = [g for g in groups.values() if before is None or g["newest"] < before]
    page, rest = rows[:limit], rows[limit:]
    for g in page:
        g["items"] = [thread_view(c, t) for t in g["items"]]
    return page, (page[-1]["newest"] if rest else None)


def thread(c: sqlite3.Connection, tid: int, cid: str | None = None) -> list[dict]:
    """Every message of thread `tid`, oldest first, cancelled ones marked."""
    return [{**view(c, r["id"], cid), "text": r["text"],
             "cancelled": r["cancelled_at"] is not None}
            for r in c.execute("SELECT id, text, cancelled_at FROM intercom WHERE thread=? "
                               "ORDER BY id", (tid,))]


def standing(c: sqlite3.Connection) -> list[dict]:
    """Announcements to everyone whose `until` is still ahead, oldest first."""
    return [{**view(c, r["id"]), "text": r["text"]} for r in c.execute(
        "SELECT id, text FROM intercom WHERE to_addr='all' AND until_at > ? AND "
        "cancelled_at IS NULL ORDER BY id", (now(),))]
