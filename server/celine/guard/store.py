"""Bastet's SQL: every query on guard_decisions, kv and intercom the guard makes is here
(the guard's other modules read and write only through it), plus the open freeze.

Bastet's decisions: one row per rule that had something to answer on a call (code found
a reason to ask). Durable: the events table is pruned, this one is not (migrations 0110,
0112).

decision: allow (asked, the judge said no; a page check's mark), stop, would_stop (Watch,
or a rule set to watch: it would have acted), found (a patrol finding the chain
confirmed), passed (a pass covered it), asked, approved,
denied, closed (a confirm ask nobody answered in time), waiting (the judge has not
answered yet), failed (it broke), unreachable (no judge could answer: counted apart
from a stop and from a failure). `froze`: this decision froze it, or would have in
Watch. Every row that is not a silent allow is a banner in its conversation
(view.py): writing or settling one emits its `guard.banner` event (`emit`). A banner's
thread with the owner lives in kv `guard.thread.<row id>` (banners.py answers in it).

The page check (guard/page.py) writes rule injection on the result's call; the patrol
(guard/patrol.py) writes `does` patrol, one row per rule it found on a finished turn:
tool "patrol", tool_call_id the turn's id, message_id its last reply in it.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from ..core.event_types import GUARD_BANNER
from ..core.ids import iso, now
from ..events import added
from . import bastet, view
from .rules import REPEATABLE

DECISIONS = ("waiting", "allow", "would_stop", "stop", "failed", "unreachable", "found",
             "closed", "asked", "approved", "denied", "passed")
_COLS = ("id", "conversation_id", "turn_id", "message_id", "tool_call_id", "tool", "mode",
         "layer", "rule", "does", "decision", "link", "source", "quote", "why", "class",
         "sink", "piece", "froze", "p", "scores", "model", "ms", "created_at")
PIECE = 300


def _row(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in _COLS}
    d["scores"] = json.loads(d["scores"]) if d["scores"] else None
    d["froze"] = bool(d["froze"])
    return d


async def begin(store, *, turn: dict, call: dict, mode: str, link: str,
                source: str | None, rule: str = "hijack", does: str | None = None) -> int:
    """The row of a call the judge is about to look at: "waiting" until settle."""
    def fn(c: sqlite3.Connection) -> int:
        return c.execute(
            "INSERT INTO guard_decisions (conversation_id, turn_id, message_id, tool_call_id, "
            "tool, mode, layer, rule, does, decision, link, source, created_at) "
            "VALUES (?,?,?,?,?,?,2,?,?,'waiting',?,?,?)",
            (turn["conversation_id"], turn["id"], call.get("message_id"), call["id"],
             call["name"], mode, rule, does, link, source, now())).lastrowid
    return await store.write(fn)


async def settle(store, rid: int, *, decision: str, source: str | None, quote: str,
                 why: str, scores: dict | None, model: str | None, ms: int,
                 p: float | None = None) -> int:
    """The judge answered. -> the turn's stopped calls so far."""
    def fn(c: sqlite3.Connection) -> int:
        r = c.execute(
            "UPDATE guard_decisions SET decision=?, source=coalesce(?, source), quote=?, "
            "why=?, scores=?, model=?, ms=?, p=? WHERE id=? RETURNING turn_id",
            (decision, source, quote or None, why or None,
             json.dumps(scores) if scores else None, model, ms, p, rid)).fetchone()
        emit(c, store, rid)
        return _stops(c, r[0])
    return await store.write(fn)


async def record(store, *, turn: dict, call: dict, mode: str, rule: str, does: str | None,
                 decision: str, layer: int = 2, link: str | None = None,
                 source: str | None = None, quote: str = "", why: str = "",
                 cls: str | None = None, sink: str | None = None,
                 piece: str | None = None, p: float | None = None,
                 model: str | None = None, ms: int | None = None) -> int:
    """One row, written whole (every rule but hijack)."""
    def fn(c: sqlite3.Connection) -> int:
        rid = c.execute(
            "INSERT INTO guard_decisions (conversation_id, turn_id, message_id, tool_call_id, "
            "tool, mode, layer, rule, does, decision, link, source, quote, why, class, sink, "
            "piece, p, model, ms, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (turn["conversation_id"], turn["id"], call.get("message_id"), call["id"],
             call["name"], mode, layer, rule, does, decision, link, source, quote or None,
             why or None, cls, sink,
             (piece or "")[:PIECE] or None, p, model, ms, now())).lastrowid
        emit(c, store, rid)
        return rid
    return await store.write(fn)


async def mark(store, rid: int, *, decision: str | None = None,
               froze: bool | None = None) -> None:
    """A row's later turn: a confirm answered, a decision that froze it."""
    def fn(c: sqlite3.Connection) -> None:
        c.execute("UPDATE guard_decisions SET decision=coalesce(?, decision), "
                  "froze=coalesce(?, froze) WHERE id=?",
                  (decision, None if froze is None else int(froze), rid))
        emit(c, store, rid)
    await store.write(fn)


async def close_asked(store) -> int:
    """At startup: a confirm ask the last run left open is closed (requests live in
    memory, approvals.py; nothing waits for them any more), its banner without Approve
    and Deny. -> how many."""
    def fn(c: sqlite3.Connection) -> int:
        rids = [r[0] for r in c.execute(
            "UPDATE guard_decisions SET decision='closed' WHERE decision='asked' "
            "RETURNING id").fetchall()]
        for rid in rids:
            emit(c, store, rid)
        return len(rids)
    return await store.write(fn)


def _stops(c: sqlite3.Connection, turn_id: str, decision: str = "stop") -> int:
    """Calls of this turn a rule stopped (`decision` would_stop: would have, in Watch),
    the stops rule's own rows aside."""
    return c.execute("SELECT count(DISTINCT tool_call_id) FROM guard_decisions WHERE "
                     "turn_id=? AND decision=? AND rule <> 'stops'",
                     (turn_id, decision)).fetchone()[0]


async def injected(store, turn_id: str) -> bool:
    """Did the page check mark words aimed at an AI in this turn? Its mark raises the
    stakes of its next calls (guard.check)."""
    return await store.read(lambda c: c.execute(
        "SELECT 1 FROM guard_decisions WHERE turn_id=? AND rule='injection' AND "
        "does='mark' LIMIT 1", (turn_id,)).fetchone() is not None)


async def stopped(store, conversation_id: str, since: str, until: str, call_id: str,
                  decision: str = "stop") -> list[dict]:
    """The calls a rule stopped (`decision` would_stop: would have, in Watch) in this
    conversation between `since` and `until`, newest first, each row with the stopped
    call's args (from tool_calls; {} when it has no row): what persist compares a new
    call with. Not the call `call_id` itself."""
    marks = ",".join("?" * len(REPEATABLE))

    def fn(c: sqlite3.Connection) -> list[dict]:
        out = []
        for r in c.execute(
                "SELECT g.rule, g.tool, g.sink, g.piece, g.quote, g.created_at, "
                "t.arguments FROM guard_decisions g LEFT JOIN tool_calls t ON "
                "t.id = g.tool_call_id WHERE g.conversation_id=? AND g.decision=? AND "
                f"g.rule IN ({marks}) AND g.created_at >= ? AND g.created_at <= ? AND "
                "g.tool_call_id <> ? ORDER BY g.id DESC LIMIT 20",
                (conversation_id, decision, *REPEATABLE, since, until, call_id)):
            d = dict(r)
            d["args"] = args_of(d.pop("arguments"))
            out.append(d)
        return out
    return await store.read(fn)


async def retries(store, conversation_id: str, since: str, piece: str,
                  decisions: tuple) -> int:
    """How many tries at the stop made at `since` persist already acted on here: its
    rows since then on the same `piece` (another stop's tries do not count)."""
    marks = ",".join("?" * len(decisions))
    return await store.read(lambda c: c.execute(
        "SELECT count(*) FROM guard_decisions WHERE conversation_id=? AND rule='persist' "
        f"AND decision IN ({marks}) AND created_at >= ? AND piece=?",
        (conversation_id, *decisions, since, piece)).fetchone()[0])


async def chain(store, rid: int, entry: dict) -> None:
    """One more level's verdict on row `rid` (guard/chain.py): appended to scores
    "chain"; L1's own is the row itself (quote, why, model)."""
    def fn(c: sqlite3.Connection) -> None:
        r = c.execute("SELECT scores FROM guard_decisions WHERE id=?", (rid,)).fetchone()
        if r is None:
            return
        s = json.loads(r[0] or "{}") or {}
        s["chain"] = [*(s.get("chain") or []), entry]
        c.execute("UPDATE guard_decisions SET scores=? WHERE id=?", (json.dumps(s), rid))
        emit(c, store, rid)
    await store.write(fn)


def table_of(row) -> str:
    """The questions.toml table a row's question is in: the patrol's [after], else [ask]."""
    return "after" if row["does"] == "patrol" else "ask"


def levels(row: dict) -> list[dict]:
    """The chain on a row, first look first: L1 (the row's own verdict, quote and why),
    then what scores "chain" holds."""
    first = {"level": "L1", "verdict": "Y" if row.get("quote") or row.get("why") else
             "N" if row.get("decision") == "allow" else "", "quote": row.get("quote") or "",
             "why": row.get("why") or "", "model": row.get("model") or ""}
    return [first, *((row.get("scores") or {}).get("chain") or [])]


async def told(store, conversation_id: str, rid: int, hours: int) -> bool:
    """Was the owner told already of the same rule and evidence (quote, else piece) as
    row `rid` in this conversation within `hours`: an earlier stop or patrol finding."""
    def fn(c: sqlite3.Connection) -> bool:
        r = c.execute("SELECT rule, coalesce(quote, piece, '') AS ev, created_at FROM "
                      "guard_decisions WHERE id=?", (rid,)).fetchone()
        if r is None:
            return False
        since = iso(datetime.fromisoformat(r["created_at"]) - timedelta(hours=hours))
        return c.execute(
            "SELECT 1 FROM guard_decisions WHERE conversation_id=? AND rule=? AND "
            "coalesce(quote, piece, '')=? AND (decision='stop' OR (does='patrol' AND "
            "decision='found')) AND "
            "id<>? AND created_at>=? LIMIT 1",
            (conversation_id, r["rule"], r["ev"], rid, since)).fetchone() is not None
    return await store.read(fn)


async def stops(store, turn_id: str, decision: str = "stop") -> int:
    return await store.read(lambda c: _stops(c, turn_id, decision))


async def listing(store, *, decision: str | None = None, source: str | None = None,
                  turn: str | None = None, rule: str | None = None,
                  mode: str | None = None, limit: int = 100,
                  before: int | None = None) -> list[dict]:
    """The newest first; `source` matches part of the source."""
    where, args = [], []
    for sql, v in (("decision = ?", decision), ("instr(source, ?) > 0", source),
                   ("turn_id = ?", turn), ("rule = ?", rule), ("mode = ?", mode),
                   ("id < ?", before)):
        if v is not None:
            where.append(sql)
            args.append(v)
    q = ("SELECT * FROM guard_decisions" + (" WHERE " + " AND ".join(where) if where else "")
         + " ORDER BY id DESC LIMIT ?")
    return await store.read(lambda c: [_row(r) for r in c.execute(q, [*args, limit])])


async def one(store, rid: int) -> dict | None:
    """One decision whole, for its detail view: the row, the call's `arguments` (from
    tool_calls; {} when it has no row), Bastet's `line` and `kind` as her banner says
    them, and the `actions` the owner can take on it (view.actions). None: no row."""
    def fn(c: sqlite3.Connection) -> dict | None:
        r = c.execute("SELECT g.*, t.arguments FROM guard_decisions g LEFT JOIN tool_calls "
                      "t ON t.id = g.tool_call_id WHERE g.id=?", (rid,)).fetchone()
        if r is None:
            return None
        d = _row(r)
        d["arguments"] = args_of(r["arguments"])
        d.update(kind=view.kind(r), line=view.line(r), actions=view.actions(r))
        return d
    return await store.read(fn)


def of_message(c: sqlite3.Connection, message_id: str) -> list[dict]:
    """The decisions on the calls of one reply step, in call order (What it saw)."""
    return [_row(r) for r in c.execute(
        "SELECT * FROM guard_decisions WHERE message_id=? ORDER BY id", (message_id,))]


def row(c: sqlite3.Connection, rid: int | None) -> sqlite3.Row | None:
    """One decision row as it is stored, None without one."""
    if rid is None:
        return None
    return c.execute("SELECT * FROM guard_decisions WHERE id=?", (rid,)).fetchone()


def args_of(text: str | None) -> dict:
    """A tool_calls row's arguments JSON as a dict ({} when it is none or not one)."""
    try:
        got = json.loads(text or "{}")
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def call_args(c: sqlite3.Connection, tool_call_id: str) -> dict:
    """A call's arguments as stored ({} when it has no row)."""
    r = c.execute("SELECT arguments FROM tool_calls WHERE id=?", (tool_call_id,)).fetchone()
    return args_of(r[0]) if r else {}


async def close_ask(store, rid: int) -> None:
    """An ask nothing waits for any more is closed: its banner without Approve and Deny."""
    def fn(c: sqlite3.Connection) -> None:
        if c.execute("UPDATE guard_decisions SET decision='closed' WHERE id=? AND "
                     "decision='asked'", (rid,)).rowcount:
            emit(c, store, rid)
    await store.write(fn)


# ---- kv: the guard's keys --------------------------------------------------------------

def kv(c: sqlite3.Connection, k: str) -> str | None:
    r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def kv_set(c: sqlite3.Connection, k: str, v: str) -> None:
    c.execute("INSERT OR REPLACE INTO kv(k, v) VALUES(?,?)", (k, v))


def kv_del(c: sqlite3.Connection, k: str) -> bool:
    return c.execute("DELETE FROM kv WHERE k=?", (k,)).rowcount > 0


FROZEN = "frozen"                # the open freeze's id (freeze.py)


def open_freeze(c: sqlite3.Connection) -> dict | None:
    """The open freeze, None while it is not frozen."""
    k = kv(c, FROZEN)
    r = c.execute("SELECT * FROM freezes WHERE id=?", (int(k),)).fetchone() if k else None
    return dict(r) if r else None


ONCE = "guard.once."             # a call the owner allowed once: <conversation>.<call>


async def allow_once(store, conversation_id: str, call: str, rid: int) -> None:
    await store.write(lambda c: kv_set(c, f"{ONCE}{conversation_id}.{call}", str(rid)))


async def take_once(store, conversation_id: str, call: str) -> bool:
    """True once when the owner allowed this call once here, the allowance used. A read
    first: most calls have none, and they need no write."""
    key = f"{ONCE}{conversation_id}.{call}"
    if await store.read(lambda c: kv(c, key)) is None:
        return False
    return await store.write(lambda c: kv_del(c, key))


# ---- the banners: rows that show in their conversation ------------------------------------

THREAD = "guard.thread."         # a banner's thread: [{by, text, at, grant?}]
_SHOWN = (f"(mode='on' OR rule='gates') AND decision IN "
          f"({','.join(repr(d) for d in view.BANNER_DECISIONS)})")


def thread(c: sqlite3.Connection, rid: int) -> list[dict]:
    k = kv(c, f"{THREAD}{rid}")
    return json.loads(k) if k else []


def kept(c: sqlite3.Connection, rid: int) -> bool:
    """The owner answered keep on this decision's banner."""
    return any((x.get("grant") or {}).get("grant") == "keep" for x in thread(c, rid))


def _frozen_turn(c: sqlite3.Connection) -> str | None:
    return (open_freeze(c) or {}).get("turn_id")


def banner(c: sqlite3.Connection, rid: int) -> dict | None:
    """The banner of row `rid`, None when it shows none."""
    r = row(c, rid)
    return view.banner(r, thread(c, rid), _frozen_turn(c)) if view.shown(r) else None


def banners(c: sqlite3.Connection, cid: str) -> list[dict]:
    """The conversation's banners, oldest first (the snapshot); a repeat collapses into
    the first that says the same, unless it froze it."""
    held = _frozen_turn(c)
    out: list[dict] = []
    for r in c.execute(f"SELECT * FROM guard_decisions WHERE conversation_id=? AND {_SHOWN} "
                       "ORDER BY id", (cid,)):
        b = view.banner(r, thread(c, r["id"]), held)
        if b["frozen"] or not any(view.repeats(b, o) for o in out):
            out.append(b)
    return out


def _repeat(c: sqlite3.Connection, b: dict) -> bool:
    """An earlier banner of this conversation says the same: this one collapses into it."""
    return any(view.repeats(b, {"rule": r["rule"], "kind": view.kind(r), "line": view.line(r)})
               for r in c.execute(
                   f"SELECT * FROM guard_decisions WHERE conversation_id=? AND id<? AND "
                   f"{_SHOWN} AND rule=?", (b["conversation_id"], b["id"], b["rule"])))


def emit(c: sqlite3.Connection, store, rid: int) -> None:
    """Inside a write: the banner of row `rid` to its conversation, when it shows. A row
    of a conversation that is gone (or never was, a script's) emits nothing."""
    b = banner(c, rid)
    if b is None or (not b["frozen"] and _repeat(c, b)):
        return
    added(store, c, "guard", b["conversation_id"])
    try:
        store.event(c, b["conversation_id"], GUARD_BANNER, {"banner": b}, touch=False)
    except KeyError:
        pass


def emit_turn(c: sqlite3.Connection, store, turn_id: str | None) -> None:
    """Inside a write: the freeze banners of a turn (a freeze began or ended)."""
    if turn_id:
        for (rid,) in c.execute("SELECT id FROM guard_decisions WHERE turn_id=? AND froze=1",
                                (turn_id,)).fetchall():
            emit(c, store, rid)


async def add_to_thread(store, rid: int, lines: list[dict]) -> dict | None:
    """The owner's words and Bastet's line, last in the banner's thread. -> the banner."""
    def fn(c: sqlite3.Connection) -> dict | None:
        kv_set(c, f"{THREAD}{rid}", json.dumps(thread(c, rid) + lines, ensure_ascii=False))
        emit(c, store, rid)
        return banner(c, rid)
    return await store.write(fn)


# ---- what Bastet reads back ---------------------------------------------------------------

def decided(c: sqlite3.Connection, n: int) -> list[sqlite3.Row]:
    """Bastet's newest n decisions Celine heard of (mode On: a stop, an ask and how it ended,
    a patrol finding), newest first."""
    return c.execute(
        "SELECT * FROM guard_decisions WHERE mode='on' AND (decision IN ('stop','asked',"
        "'approved','denied','closed') OR (does='patrol' AND decision='found')) "
        "ORDER BY id DESC LIMIT ?", (n,)).fetchall()


def like(c: sqlite3.Connection, freeze_id: int, sig: dict) -> list[sqlite3.Row]:
    """Bastet's freezes before `freeze_id` whose decision has the kind `sig` (rule, tool,
    sink, cls), newest first (learn.py)."""
    return c.execute(
        "SELECT f.id, f.at, f.unfrozen_at, f.unfrozen_by, f.decision_id FROM freezes f "
        "JOIN guard_decisions g ON g.id = f.decision_id WHERE f.by='guard' AND f.id < ? "
        "AND g.rule=? AND g.tool=? AND g.sink IS ? AND g.class IS ? ORDER BY f.id DESC",
        (freeze_id, sig["rule"], sig["tool"], sig["sink"], sig["cls"])).fetchall()


PATROL_AT = "patrol_at"          # when the last patrol read


async def patrol_at(store) -> str:
    return await store.read(lambda c: kv(c, PATROL_AT)) or ""


async def patrolled(store, at: str) -> None:
    await store.write(lambda c: kv_set(c, PATROL_AT, at))


async def finished(store, since: str, until: str, states: tuple[str, ...]) -> list[dict]:
    """The turns that finished between `since` and `until` the patrol has not looked at,
    in chats that keep Bastet on, oldest first."""
    marks = ",".join("?" * len(states))
    return await store.read(lambda c: [dict(r) for r in c.execute(
        f"SELECT id, conversation_id, finished_at FROM turns WHERE state IN ({marks}) "
        "AND finished_at > ? AND finished_at <= ? "
        "AND id NOT IN (SELECT tool_call_id FROM guard_decisions WHERE does='patrol') "
        "AND conversation_id NOT IN (SELECT id FROM conversations WHERE guard_off=1) "
        "ORDER BY finished_at", (*states, since, until))])


# ---- the intercom: Bastet's threads ---------------------------------------------------------

def to_bastet(c: sqlite3.Connection, iid: int) -> sqlite3.Row | None:
    """Celine's message #iid to Bastet, while it waits for her answer; None once it is
    gone, cancelled or answered."""
    m = c.execute("SELECT * FROM intercom WHERE id=?", (iid,)).fetchone()
    if m is None or m["cancelled_at"] or not m["from_addr"].startswith("agent:") or \
            not c.execute("SELECT 1 FROM intercom_deliveries WHERE intercom_id=? AND "
                          "to_addr=? AND state='pending'", (iid, bastet.ADDR)).fetchone():
        return None
    return m


def talk(c: sqlite3.Connection, iid: int, n: int) -> list[tuple[str, str]]:
    """Celine's and Bastet's last n messages before #iid, oldest first: (who, text), who
    "her" or "bastet"."""
    rows = c.execute(
        "SELECT from_addr, text FROM intercom WHERE id < ? AND cancelled_at IS NULL AND "
        "((from_addr LIKE 'agent:%' AND instr(to_addr, ?) > 0) OR "
        "(from_addr = ? AND to_addr LIKE 'agent:%')) ORDER BY id DESC LIMIT ?",
        (iid, bastet.ADDR, bastet.ADDR, n)).fetchall()
    return [("bastet" if r["from_addr"] == bastet.ADDR else "her", r["text"])
            for r in reversed(rows)]


def answered(c: sqlite3.Connection, iid: int) -> bool:
    """Bastet answered #iid already."""
    return c.execute("SELECT 1 FROM intercom WHERE from_addr=? AND reply_to=?",
                     (bastet.ADDR, iid)).fetchone() is not None


async def ever_wrote_her(store) -> bool:
    """Bastet ever wrote to Celine (her first message says who she is)."""
    return await store.read(lambda c: c.execute(
        "SELECT 1 FROM intercom WHERE from_addr=? AND to_addr LIKE 'agent:%' LIMIT 1",
        (bastet.ADDR,)).fetchone() is not None)


async def read_by_bastet(store, iid: int) -> None:
    await store.write(lambda c: c.execute(
        "UPDATE intercom_deliveries SET state='read', delivered_at=? WHERE intercom_id=? "
        "AND to_addr=? AND state='pending'", (now(), iid, bastet.ADDR)))


def last_to_her(c: sqlite3.Connection, conversation_id: str, since: str) -> sqlite3.Row | None:
    """Bastet's newest message to Celine in this conversation since `since`."""
    return c.execute("SELECT id, thread, created_at FROM intercom WHERE from_addr=? AND "
                     "to_addr=? AND created_at >= ? AND cancelled_at IS NULL ORDER BY id DESC "
                     "LIMIT 1", (bastet.ADDR, f"agent:{conversation_id}", since)).fetchone()


def about(c: sqlite3.Connection, iid: int) -> int | None:
    """The decision Bastet's newest note in the thread of message #iid is about."""
    for (data,) in c.execute(
            "SELECT data FROM intercom WHERE from_addr=? AND thread = (SELECT thread FROM "
            "intercom WHERE id=?) ORDER BY id DESC", (bastet.ADDR, iid)):
        if (rid := (json.loads(data or "{}") or {}).get("decision_id")) is not None:
            return int(rid)
    return None
