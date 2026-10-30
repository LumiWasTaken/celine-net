"""Turn rows and tool call rows: plain functions over the store. The state machine
(claim, transition, put down, the startup sweep, continue, the abort flag), where a
turn carries on, its usage, and the journal of its tool calls."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
import zlib

from .. import attachments as files
from ..conversations import tree, view
from ..core.ids import new_id, now
from ..core.states import LIVE, LIVE_SQL, QUEUE_ORDER, TERMINAL
from ..errors import mask_all
from ..guard import mark
from ..skills import words
from .. import homes
from ..intercom.store import NAMES, insert as intercom_insert
from ..plans import store as plans
from .journal import restarted, safe_to_rerun
from ..core.event_types import (
    CONTEXT_USAGE,
    MESSAGE_CREATED,
    TOOL_DONE,
    TOOL_GATED,
    TOOL_PROGRESS,
    TOOL_RESET,
    TOOL_STARTED,
    VERSION_CHANGED,
)

# why a terminal state left a tool call unsettled, in words for the placeholder result
_SETTLE_WHY = {"aborted": "stopped", "failed": "the turn failed",
               "capped": "the turn hit its limit", "done": "the turn ended",
               "interrupted": "the server restarted"}


def runs_field(c: sqlite3.Connection, store, call_ids: list[str], reset: bool = False) -> dict:
    """`runs` for the event of calls whose state changed, when their turn's runs changed
    (skills/words.py runs_after); nothing otherwise, and the client keeps the last."""
    r = words.runs_after(c, call_ids, store.tool_words, reset=reset)
    return {"runs": r} if r else {}


def tool_flags(registry):
    """name -> (mutating, mode) from the registry, None for a tool it does not know."""
    def flags(name: str):
        spec = registry.get(name) if registry is not None else None
        return (bool(spec.mutating), spec.mode) if spec is not None else None
    return flags


def content(c: sqlite3.Connection, message_id: str | None) -> str | None:
    """The text of a message, e.g. the tool row it read."""
    r = c.execute("SELECT content FROM messages WHERE id=?", (message_id,)).fetchone()
    return r["content"] if r else None


# ---- the state machine ---------------------------------------------------------------

def settle_open_calls(c: sqlite3.Connection, store, cid: str, turn_id: str,
                      why: str) -> int:
    """Every call of this turn still pending/running becomes `interrupted`, with a
    result row and a tool.done event, in the same write as the terminal state. So
    the UI never keeps a spinner under a finished turn."""
    rows = c.execute(
        "SELECT tc.id, tc.name, tc.message_id FROM tool_calls tc "
        "JOIN messages m ON m.id = tc.message_id "
        "WHERE m.turn_id = ? AND tc.state IN ('pending','running') ORDER BY tc.rowid",
        (turn_id,)).fetchall()
    if not rows:
        return 0
    leaf = tree.leaf(c, cid)
    leaf_parent = c.execute("SELECT parent_id FROM messages WHERE id=?",
                            (leaf,)).fetchone() if leaf else None
    ts = now()
    for r in rows:
        text = (f"interrupted: {why} while {r['name']} ran. The app does not know "
                f"if it finished.")
        mid = new_id()
        ordinal = tree.next_ordinal(c, r["message_id"])
        meta = tree.insert_tool_result(c, mid, cid, r["message_id"], text, ordinal, ts)
        c.execute("UPDATE tool_calls SET state='interrupted', result_message_id=?, "
                  "finished_at=? WHERE id=?", (mid, ts, r["id"]))
        # keep the visible path on this reply: the leaf follows its results
        if leaf == r["message_id"] or (leaf_parent is not None and
                                       leaf_parent["parent_id"] == r["message_id"]):
            tree.follow(c, cid, r["message_id"], mid)
            leaf, leaf_parent = mid, {"parent_id": r["message_id"]}
        store.event(c, cid, TOOL_DONE,
                    {"tool_call_id": r["id"], "name": r["name"], "ok": False,
                     "state": "interrupted", "reason": why, "result": text,
                     "display": None, "withheld": False,
                     "words": words.of_call(c, r["id"], "interrupted", None, text,
                                            store.tool_words),
                     # the turn's runs once, with the last: every call above has settled
                     **(runs_field(c, store, [x["id"] for x in rows]) if r is rows[-1] else {})})
        store.event(c, cid, MESSAGE_CREATED,
                    {"message_id": mid, "parent_id": r["message_id"], "role": "tool",
                     "content": text, "tool_call_id": r["id"], "meta": meta})
    return len(rows)


def set_state(c: sqlite3.Connection, store, cid: str, turn_id: str, final: str,
              reason: str | None = None, reason_code: str | None = None,
              cut: str | None = None) -> None:
    code, reason_ = reason_code, reason
    if final == "aborted":
        code = code or "stopped"
        reason_ = reason_ or "stopped"
    c.execute("UPDATE turns SET state=?, reason=?, reason_code=?, cut=?, "
              "finished_at=? WHERE id=?",
              (final, reason_, code, cut if final == "done" else None,
               now() if final in TERMINAL else None, turn_id))
    if final in TERMINAL:
        settle_open_calls(c, store, cid, turn_id, _SETTLE_WHY.get(final, "the turn ended"))
    data: dict = {"reason": reason_}
    if code:
        data["reason_code"] = code
    if cut and final == "done":
        data["cut"] = cut
    if final in TERMINAL:
        row = c.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        data["continuable"] = view.continuable(c, cid, row)
    tree.turn_state(c, store, cid, turn_id, final, **data)
    if final == "done" and not row["quiet"]:
        _reply_notice(c, store, cid, turn_id)
    # a continued reply: the conversation keeps its place in the list (tree.py)
    keep = c.execute("DELETE FROM kv WHERE k=? RETURNING v", (tree.KEEP_TIME + turn_id,)
                     ).fetchone() if final in TERMINAL else None
    if keep:
        tree.keep_time(c, cid, keep[0])


def _reply_notice(c: sqlite3.Connection, store, cid: str, turn_id: str) -> None:
    """The agent's finished reply in a chat, as a notice (events.notice): its first line. A
    mission's, a run's and a sub-agent's turns have none (a mission says when it waits
    for you, a run when it failed), nor a turn that said nothing."""
    from ..events import notice
    if c.execute("SELECT kind FROM conversations WHERE id=?", (cid,)).fetchone()[0] != "chat":
        return
    r = c.execute("SELECT content FROM messages WHERE turn_id=? AND role='assistant' AND "
                  "content <> '' ORDER BY created_at DESC LIMIT 1",
                  (turn_id,)).fetchone()
    if r is not None:
        notice(store, c, "reply", NAMES["her"], r[0], homes.of(c, cid)["link"], f"reply:{cid}")


async def claim(store, worker_id: str, models: set[str] | None = None) -> dict | None:
    """Take the next queued turn whose conversation is not already busy, in QUEUE_ORDER. A turn queued without a root answers the active leaf of now, so
    turns queued together answer one after the other on one path. A turn of
    a locked conversation (conversations/store.locked) ends aborted with the reason:
    nothing of its own starts or goes on there; a turn the user started still runs.
    Nothing while it is frozen (guard/freeze.py): the house leases its no turn.
    `models` (while she is paused, hold.py): only a sub-agent's or a drone's turn whose
    own model is one of them; every other turn waits in the queue."""
    def fn(c: sqlite3.Connection) -> dict | None:
        from ..conversations.store import locked
        from ..guard.store import open_freeze
        if open_freeze(c) is not None:
            return None
        queued = c.execute(
            "SELECT t.id, t.conversation_id, t.origin, c.agent_of, c.model_id FROM turns t "
            "JOIN conversations c ON c.id = t.conversation_id WHERE t.state='queued' "
            "  AND t.conversation_id NOT IN (SELECT conversation_id FROM turns "
            "    WHERE state IN ('preparing','streaming','tools')) "
            f"ORDER BY {QUEUE_ORDER}").fetchall()
        tid, busy = None, set()
        for r in queued:
            if r["conversation_id"] in busy:
                continue                           # its turns run in their order
            if models is not None and not (r["agent_of"] and r["model_id"] in models):
                busy.add(r["conversation_id"])
                continue
            if r["origin"] != "user" and (why := locked(c, r["conversation_id"])):
                set_state(c, store, r["conversation_id"], r["id"], "aborted", why, "locked")
                continue
            tid = r["id"]
            break
        if tid is None:
            return None
        row = c.execute(
            "UPDATE turns SET state='preparing', worker_id=?, root_message_id=coalesce("
            "root_message_id, (SELECT active_leaf_id FROM conversations WHERE "
            "id=turns.conversation_id)) WHERE id=? "
            "RETURNING id, conversation_id, root_message_id, abort_requested, origin, "
            "continues, quiet, again, cap_base, reason",
            (worker_id, tid)).fetchone()
        tree.turn_state(c, store, row["conversation_id"], row["id"], "preparing")
        return dict(row)

    return await store.write(fn)


async def transition(store, turn_id: str, cid: str, state: str,
                     reason: str | None = None, *, reason_code: str | None = None,
                     cut: str | None = None, honor_abort: bool = True) -> bool:
    """Write a state change. Returns False if an abort was requested, so the
    caller stops. A terminal state settles the calls the turn left open.
    honor_abort=False: the answer was complete before the Stop (only the citation
    check was cancelled), so the turn ends as asked."""
    def fn(c: sqlite3.Connection) -> bool:
        aborted = bool(c.execute("SELECT abort_requested FROM turns WHERE id=?",
                                 (turn_id,)).fetchone()["abort_requested"])
        final = state
        if aborted and honor_abort and state in ("done",) + LIVE:
            final = "aborted"
        set_state(c, store, cid, turn_id, final, reason, reason_code, cut)
        if final == "done":
            plans.finish_sync(c, store, cid)     # its answer finished the last step
        return not aborted or not honor_abort

    return await store.write(fn)


async def fail_if_active(store, turn_id: str, cid: str, reason: str,
                         reason_code: str | None = None) -> bool:
    """Terminal 'failed' only if the turn is still live; a turn that finished
    on its own keeps its state."""
    def fn(c: sqlite3.Connection) -> bool:
        live = c.execute(f"SELECT 1 FROM turns WHERE id=? AND state IN {LIVE_SQL}",
                         (turn_id,)).fetchone()
        if live:
            set_state(c, store, cid, turn_id, "failed", reason, reason_code)
        return bool(live)

    return await store.write(fn)


async def put_down(store, worker_ids: list[str], reason: str | None = None,
                   code: str | None = None) -> list[str]:
    """Turns these workers claimed but did not run (a freeze, the shutdown before the
    run began) go back in the queue."""
    if not worker_ids:
        return []

    def fn(c: sqlite3.Connection) -> list[str]:
        marks = ",".join("?" * len(worker_ids))
        rows = c.execute(
            f"UPDATE turns SET state='queued', reason=?, reason_code=?, worker_id=NULL "
            f"WHERE worker_id IN ({marks}) AND state IN ('preparing','streaming','tools') "
            f"RETURNING id, conversation_id", (reason, code, *worker_ids)).fetchall()
        for r in rows:
            tree.turn_state(c, store, r["conversation_id"], r["id"], "queued",
                            reason=reason, reason_code=code)
        return [r["id"] for r in rows]

    return await store.write(fn)


def _cut(c: sqlite3.Connection, store, rows, registry) -> None:
    """Live turns the server stopped under ends `interrupted` with the reason; its steps
    in flight with them. Nothing runs again on its own: Regenerate is the user's."""
    flags = tool_flags(registry)
    for row in rows:
        open_calls = c.execute(
            "SELECT tc.name, tc.state, tc.mutating, tc.mode FROM tool_calls tc "
            "JOIN messages m ON m.id = tc.message_id WHERE m.turn_id = ? AND "
            "tc.state IN ('pending','running','detached')", (row["id"],)).fetchall()
        why = restarted(open_calls, flags) or "The server restarted during the answer."
        c.execute("UPDATE turns SET worker_id=NULL WHERE id=?", (row["id"],))
        set_state(c, store, row["conversation_id"], row["id"], "interrupted", why, "restart")


async def interrupt_held(store, worker_ids: list[str], registry=None) -> list[str]:
    """Shutdown: the turns these workers run end `interrupted` (_cut)."""
    if not worker_ids:
        return []

    def fn(c: sqlite3.Connection) -> list[str]:
        marks = ",".join("?" * len(worker_ids))
        rows = c.execute(
            f"SELECT id, conversation_id FROM turns WHERE worker_id IN ({marks}) AND "
            f"state IN ('preparing','streaming','tools')", worker_ids).fetchall()
        _cut(c, store, rows, registry)
        return [r["id"] for r in rows]

    return await store.write(fn)


async def sweep(store, registry=None) -> dict:
    """At startup, before the workers start: turns a crash left running end
    `interrupted` (_cut); queued ones wait for a worker as before. Running jobs end
    `interrupted`, and a conversation one of them was cut in gets one notice."""
    def fn(c: sqlite3.Connection) -> dict:
        live = c.execute("SELECT id, conversation_id FROM turns WHERE state IN "
                         "('preparing','streaming','tools')").fetchall()
        _cut(c, store, live, registry)
        c.execute("UPDATE turns SET worker_id=NULL WHERE state='queued'")
        from ..jobs.store import interrupt_running
        jobs = interrupt_running(c, store)
        cut: dict[str, list[str]] = {}
        for cid, line in jobs:
            cut.setdefault(cid, []).append(line)
        from ..intercom.deliver import deliver_idle_sync
        for cid, lines in cut.items():
            if c.execute("SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone():
                intercom_insert(c, "system:app", f"agent:{cid}", "notice",
                                "The app restarted and cut what was running:\n"
                                + "\n".join(lines))
                deliver_idle_sync(c, store, cid)
        return {"total": len(live) + len(jobs), "interrupted": len(live),
                "jobs": len(jobs)}

    return await store.write(fn)


async def request_abort(store, cid: str) -> str | None:
    """Idempotent and instant: sets the abort flag and returns. Does not wait for the
    turn. A turn no worker holds (still queued) ends `aborted` here at once; a held
    one ends as soon as its worker sees the cancelled token."""
    def fn(c: sqlite3.Connection) -> str | None:
        rows = c.execute(
            "UPDATE turns SET abort_requested=1 WHERE conversation_id=? AND "
            f"state IN {LIVE_SQL} RETURNING id, state, worker_id", (cid,)).fetchall()
        held = None
        for row in rows:
            if row["state"] == "queued" and row["worker_id"] is None:
                # it never ran: nothing of it shows (its cue stays on the row)
                set_state(c, store, cid, row["id"], "aborted", reason_code="not_started")
            else:
                held = str(row["id"])
        return held or (str(rows[0]["id"]) if rows else None)

    return await store.write(fn)


async def continue_turn(store, cid: str, turn_id: str, registry=None, *,
                        wait: bool = False, prefill: bool = True) -> tuple[str, int]:
    """Put a finished-but-unfinished turn back in the queue, as the same turn.
    `wait`: Keep waiting, its queued reason `keep_waiting`: a busy provider is waited
    for (backends/llm.py). `prefill`: whether its model takes one; a turn whose tail is
    a cut reply is refused before anything is written when it does not (the runner
    ends such a turn with no_prefill for a caller that does not ask first).
    Read-only calls it left interrupted or cancelled run again with the same call
    id; a mutating one keeps its `interrupted` result, never re-run silently.
    Raises KeyError (no such turn), tree.Busy, or ValueError(reason)."""
    flags = tool_flags(registry)

    def fn(c: sqlite3.Connection) -> tuple[str, int]:
        turn = c.execute("SELECT * FROM turns WHERE id=? AND conversation_id=?",
                         (turn_id, cid)).fetchone()
        if turn is None:
            raise KeyError(turn_id)
        if c.execute(f"SELECT 1 FROM turns WHERE conversation_id=? AND state IN {LIVE_SQL}",
                     (cid,)).fetchone():
            raise tree.Busy()
        verdict = view.continuable(c, cid, turn)
        if not verdict["ok"]:
            raise ValueError(verdict["reason"])
        # the tail reply's interrupted/cancelled read-only calls run again
        tail = tree.turn_tail(c, turn_id, turn["root_message_id"], tree.leaf(c, cid)) or []
        asst = next((m for m in tail if m["role"] == "assistant"), None)
        # a cut reply continues in place (resume_point reopen): only with a prefill
        last = tail[0] if tail else None
        if not prefill and last is not None and last["role"] == "assistant" \
                and last["content"] and last["finish_reason"] != "stop" \
                and c.execute("SELECT 1 FROM tool_calls WHERE message_id=?",
                              (last["id"],)).fetchone() is None:
            raise ValueError(tree.NO_PREFILL)
        reran: list[str] = []
        if asst is not None:
            for r in c.execute(
                    "SELECT id, name, state, mutating, mode, result_message_id "
                    "FROM tool_calls WHERE message_id=? AND state IN "
                    "('interrupted','cancelled') ORDER BY rowid", (asst["id"],)).fetchall():
                if not safe_to_rerun("pending", r["mutating"], r["mode"], flags(r["name"])):
                    continue
                if r["result_message_id"]:
                    tree.delete_row(c, r["result_message_id"])
                c.execute("UPDATE tool_calls SET state='pending', result_message_id=NULL,"
                          " withheld_output=NULL, finished_at=NULL WHERE id=?", (r["id"],))
                reran.append(r["id"])
            if reran:
                # the leaf may have been one of the deleted rows
                row = c.execute("SELECT active_leaf_id FROM conversations WHERE id=?",
                                (cid,)).fetchone()
                if row["active_leaf_id"] is None or c.execute(
                        "SELECT 1 FROM messages WHERE id=?",
                        (row["active_leaf_id"],)).fetchone() is None:
                    keep = c.execute(
                        "SELECT result_message_id FROM tool_calls WHERE message_id=? "
                        "AND result_message_id IS NOT NULL ORDER BY rowid DESC LIMIT 1",
                        (asst["id"],)).fetchone()
                    new_leaf = keep[0] if keep else asst["id"]
                    tree.set_leaf(c, cid, new_leaf)
                    tree.prefer(c, asst["id"], new_leaf if new_leaf != asst["id"] else None)
                    store.event(c, cid, VERSION_CHANGED,
                                {"parent_id": asst["id"], "active_leaf_id": new_leaf})
        continues = int(turn["continues"] or 0) + 1
        # a capped turn gets a fresh budget of steps from where it stands
        base = _next_iteration(c, turn_id) if turn["state"] == "capped" else \
            int(turn["cap_base"] or 0)
        why = "keep_waiting" if wait else "continued"
        c.execute("UPDATE turns SET state='queued', reason=?, reason_code=NULL,"
                  " abort_requested=0, finished_at=NULL, worker_id=NULL, continues=?,"
                  " cut=NULL, cap_base=? WHERE id=?", (why, continues, base, turn_id))
        # the turn's runs once, with the last of them: these calls are at work again
        for i, call_id in enumerate(reran):
            store.event(c, cid, TOOL_RESET, {
                "tool_call_id": call_id, "state": "pending",
                **(runs_field(c, store, reran, reset=True) if i == len(reran) - 1 else {})})
        seq = tree.turn_state(c, store, cid, turn_id, "queued", reason=why,
                              continues=continues)
        return turn_id, seq

    return await store.write(fn)


def call_id(turn_id: str, iteration: int, index: int) -> str:
    """A tool call's id, deterministic, so a step re-executed after a crash collides
    with its own (existing_tool_result); _next_iteration reads it back."""
    return f"{turn_id}:{iteration}:{index}"


def _next_iteration(c: sqlite3.Connection, turn_id: str) -> int:
    """The step number after the turn's last step with calls (its call ids say it,
    call_id)."""
    n = 0
    for r in c.execute("SELECT tc.id FROM tool_calls tc JOIN messages m ON "
                       "m.id = tc.message_id WHERE m.turn_id = ?", (turn_id,)):
        parts = str(r["id"]).split(":")
        if len(parts) >= 3 and parts[1].isdigit():
            n = max(n, int(parts[1]) + 1)
    return n


async def resume_point(store, turn_id: str) -> dict:
    """Where a turn that already ran carries on: the next iteration number (so call
    ids never collide with the journal), the message to parent the next step on
    (the turn's own tail on the active path, never a fork off the user message),
    calls in flight to run again, and a reply without calls to continue in place."""
    def fn(c: sqlite3.Connection) -> dict:
        t = c.execute("SELECT conversation_id, root_message_id FROM turns WHERE id=?",
                      (turn_id,)).fetchone()
        if t is None:
            raise KeyError(turn_id)
        root = t["root_message_id"]
        out = {"parent": root, "iteration": _next_iteration(c, turn_id), "reopen": None,
               "pending": [], "complete": None}
        leaf = tree.leaf(c, t["conversation_id"])
        tail = tree.turn_tail(c, turn_id, root, leaf)
        if not tail:
            return out
        last = tail[0]
        asst = next((m for m in tail if m["role"] == "assistant"), None)
        calls = [dict(r) for r in c.execute(
            "SELECT id, name, arguments, state, result_message_id, message_id, "
            "mutating, mode FROM tool_calls WHERE message_id=? ORDER BY rowid",
            ((asst or {}).get("id"),))] if asst else []
        if last["role"] == "assistant" and not calls:
            if last["content"] and last["finish_reason"] == "stop":
                # the reply had streamed to its end: nothing to continue
                out["complete"] = {"message_id": last["id"], "text": last["content"],
                                   "reasoning": last["reasoning"]}
                return out
            # cut off: the row holds the text streamed so far
            out["reopen"] = {"message_id": last["id"], "text": last["content"] or ""}
            out["parent"] = last["parent_id"]
            out["iteration"] = max(out["iteration"], sum(
                1 for m in tail[1:] if m["role"] == "assistant"))
            return out
        out["pending"] = [r for r in calls if r["state"] in ("pending", "running")
                          and not r["result_message_id"]]
        out["parent"] = leaf
        return out

    return await store.read(fn)


async def set_backend(store, turn_id: str, backend_id: str, model: str) -> None:
    await store.write(lambda c: c.execute(
        "UPDATE turns SET backend_id=?, model=? WHERE id=?", (backend_id, model, turn_id)))


# ---- usage ---------------------------------------------------------------------------

async def record_usage(store, cid: str, turn_id: str, data: dict,
                       request: dict | None = None) -> None:
    """One model call's numbers: `context.usage` in the log (not rendered), and
    turns.iterations. `request` ({provider, model, body}, the call as it went out) is
    kept in `requests` with the numbers, one row per step of the reply, for good."""
    def fn(c: sqlite3.Connection) -> None:
        c.execute("UPDATE turns SET iterations = iterations + 1 WHERE id=?", (turn_id,))
        store.event(c, cid, CONTEXT_USAGE, {"turn_id": turn_id, **data})
        if request is not None and data.get("message_id"):
            c.execute(
                "INSERT OR REPLACE INTO requests(message_id, step, turn_id, provider, "
                "model, body, estimate, prompt_tokens, completion_tokens, window, "
                "finish_reason, first_token_ms, cache_read, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (data["message_id"], int(data.get("iteration") or 0), turn_id,
                 request.get("provider", ""), request.get("model", ""),
                 pack_body(c, request.get("body")),
                 data.get("estimate"), data.get("prompt_tokens"),
                 data.get("completion_tokens"), data.get("window"),
                 data.get("finish_reason"), data.get("first_token_ms"),
                 data.get("cache_read"), now()))

    await store.write(fn)


BLOB = "blob:"     # an image in a stored request body: blob:<sha256>, its bytes in var/blobs


def pack_body(c: sqlite3.Connection, body) -> bytes:
    """A request body as `requests` keeps it: the known secrets masked (errors.mask_all,
    as raw_responses), every image whose bytes are a blob as `blob:<sha256>` (they were
    about 65% of the bytes), zlib(json)."""
    return zlib.compress(json.dumps(mask_all(_refs(c, body)), ensure_ascii=False,
                                    default=str).encode())


def _refs(c: sqlite3.Connection, v, key: str = ""):
    """The images of a wire body as blob refs, in the three shapes the backends send:
    a data URL (openai), {type: base64, data} (anthropic), `images: [b64]` (ollama). An
    image with no blob row stays as it went out."""
    if isinstance(v, dict):
        if v.get("type") == "base64" and isinstance(v.get("data"), str):
            return {**v, "data": _ref(c, v["data"])}
        return {k: _refs(c, x, k) for k, x in v.items()}
    if isinstance(v, list):
        return [_ref(c, x) if key == "images" and isinstance(x, str) else _refs(c, x)
                for x in v]
    if isinstance(v, str) and v.startswith("data:") and ";base64," in v[:100]:
        return _ref(c, v.partition(";base64,")[2], v)
    return v


def _ref(c: sqlite3.Connection, b64: str, whole: str | None = None) -> str:
    try:
        sha = hashlib.sha256(base64.b64decode(b64, validate=True)).hexdigest()
    except (binascii.Error, ValueError):
        return whole or b64
    known = c.execute("SELECT 1 FROM blobs WHERE sha256=?", (sha,)).fetchone()
    return BLOB + sha if known else whole or b64


async def requests_of(store, message_id: str) -> list[dict]:
    """Every step's request that streamed into one reply, oldest first, the body as
    it went out."""
    def fn(c: sqlite3.Connection) -> list[dict]:
        rows = c.execute("SELECT * FROM requests WHERE message_id=? ORDER BY step",
                         (message_id,)).fetchall()
        return [{**{k: r[k] for k in r.keys() if k != "body"},
                 "body": json.loads(zlib.decompress(r["body"]))} for r in rows]
    return await store.read(fn)


async def last_usage(store, cid: str) -> dict | None:
    """The newest `context.usage` event still in the log (for /usage)."""
    def fn(c: sqlite3.Connection) -> dict | None:
        r = c.execute("SELECT data, created_at FROM events WHERE conversation_id=? "
                      "AND type='context.usage' ORDER BY seq DESC LIMIT 1",
                      (cid,)).fetchone()
        return {**json.loads(r["data"]), "at": r["created_at"]} if r else None

    return await store.read(fn)


# ---- tool call rows ------------------------------------------------------------------

async def record_tool_calls(store, cid: str, message_id: str, calls: list[dict]) -> None:
    def fn(c: sqlite3.Connection) -> None:
        for call in calls:
            mut = call.get("mutating")
            c.execute(
                "INSERT INTO tool_calls(id,message_id,name,arguments,state,"
                "started_at,mutating,mode) VALUES(?,?,?,?,'pending',?,?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (call["id"], message_id, call["name"], call["arguments"], now(),
                 None if mut is None else int(bool(mut)), call.get("mode")))
            started = c.execute("SELECT started_at FROM tool_calls WHERE id=?",
                                (call["id"],)).fetchone()["started_at"]
            store.event(c, cid, TOOL_STARTED,
                        {"tool_call_id": call["id"], "message_id": message_id,
                         "name": call["name"], "arguments": call["arguments"],
                         "started_at": started,
                         "words": words.words(call["name"], call["arguments"], "running",
                                              own=store.tool_words)})

    await store.write(fn)


async def tool_started(store, cid: str, call_id: str) -> None:
    """The step runs now: in a sequential batch the second call waits for the
    first, so its clock starts here, not when the batch was recorded."""
    def fn(c: sqlite3.Connection) -> None:
        ts = now()
        row = c.execute("UPDATE tool_calls SET started_at=? WHERE id=? AND "
                        "state='pending' RETURNING name, arguments, message_id",
                        (ts, call_id)).fetchone()
        if row is not None:
            store.event(c, cid, TOOL_STARTED,
                        {"tool_call_id": call_id, "message_id": row["message_id"],
                         "name": row["name"], "arguments": row["arguments"],
                         "started_at": ts,
                         "words": words.words(row["name"], row["arguments"], "running",
                                              own=store.tool_words)})

    await store.write(fn)


async def tool_progress(store, cid: str, tool_call_id: str, note: str, **data) -> int:
    """What a running tool says it is doing: the event for the transcript, and the
    same line on the job row when the step is a job (nothing until it is one)."""
    def fn(c: sqlite3.Connection) -> int:
        c.execute("UPDATE jobs SET progress=? WHERE tool_call_id=? AND state='running'",
                  (note, tool_call_id))
        return store.event(c, cid, TOOL_PROGRESS,
                           {"tool_call_id": tool_call_id, "note": note, **data})

    return await store.write(fn)


async def finish_tool_call(store, cid: str, parent_message_id: str, call_id: str,
                           name: str, result, state: str,
                           withheld: str | None = None) -> str:
    """Write the role=tool result row and settle the call."""
    mid = new_id()

    def fn(c: sqlite3.Connection) -> str:
        content_, display, verdict = result.content, result.display, state
        done = c.execute("SELECT state, display FROM tool_calls WHERE id=?",
                         (call_id,)).fetchone()
        # the job ended before the turn wrote this row: the row keeps what it
        # reads, the call keeps the job's verdict (its result is a `job` message)
        if state == "detached" and done and done["state"] in ("ok", "error", "cancelled"):
            verdict = done["state"]
            display = json.loads(done["display"]) if done["display"] else None
        # a call whose work fills its display itself (a batch, comfyui/batches.py)
        # answers without one: the display it has stays
        elif display is None and done and done["display"]:
            display = json.loads(done["display"])
        ordinal = tree.next_ordinal(c, parent_message_id)
        meta = tree.insert_tool_result(c, mid, cid, parent_message_id, content_, ordinal,
                                       now())
        tree.follow(c, cid, parent_message_id, mid)
        for aid in getattr(result, "images", None) or ():
            c.execute("UPDATE attachments SET message_id=? WHERE id=? AND tool_call_id=?",
                      (mid, aid, call_id))
        display = files.with_files(c, call_id, display)
        ts = now()
        c.execute(
            "UPDATE tool_calls SET state=?, display=?, withheld_output=?, "
            "result_message_id=?, finished_at=? WHERE id=?",
            (verdict, json.dumps(display) if display else None, withheld, mid, ts,
             call_id))
        store.event(
            c, cid, TOOL_GATED if verdict == "gated" else TOOL_DONE,
            {"tool_call_id": call_id, "name": name, "ok": verdict == "ok",
             "state": verdict, "result": content_, "display": mark.for_client(display),
             "withheld": bool(withheld), "finished_at": ts,
             "words": words.of_call(c, call_id, verdict, display, content_, store.tool_words),
             **runs_field(c, store, [call_id])})
        store.event(c, cid, MESSAGE_CREATED,
                    {"message_id": mid, "parent_id": parent_message_id, "role": "tool",
                     "content": content_, "tool_call_id": call_id, "meta": meta})
        return mid

    return await store.write(fn)


async def step_tail(store, mid: str) -> str:
    """Where the next message of a turn hangs after reply `mid`: under its last result
    (the reply's preferred child, finish_tool_call), or under the reply itself. Always on
    the turn's own branch, never wherever the conversation's leaf is."""
    row = await store.read(lambda c: c.execute(
        "SELECT preferred_child_id FROM messages WHERE id=?", (mid,)).fetchone())
    return (row[0] if row else None) or mid


async def existing_tool_result(store, call_id: str) -> dict | None:
    """A settled journal entry for this exact step, if there is one."""
    def fn(c: sqlite3.Connection) -> dict | None:
        row = c.execute(
            "SELECT tc.state, tc.display, m.content FROM tool_calls tc "
            "LEFT JOIN messages m ON m.id = tc.result_message_id "
            "WHERE tc.id = ? AND tc.state NOT IN ('pending','running')",
            (call_id,)).fetchone()
        if row is None or row["content"] is None:
            return None
        return {"state": row["state"], "content": row["content"],
                "display": json.loads(row["display"]) if row["display"] else None}

    return await store.read(fn)
