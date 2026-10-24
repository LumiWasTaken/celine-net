"""Job rows: plain functions over the store. One shape for a job wherever it appears:
the jobs list, GET /jobs/{id}, the `jobs` of a job message. The call it ran is joined for
where it sits, when it was recorded and what it made (docs/ARCHITECTURE.md, Durable
execution)."""
from __future__ import annotations

import builtins
import json
import sqlite3

from ..core.ids import now
from ..intercom.store import insert as intercom_insert
from ..turns import store as turns
from ..skills import words
from ..core.event_types import JOB_STATE, TOOL_DETACHED, TOOL_DONE


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
    if isinstance(items, builtins.list) and items and isinstance(items[0], dict):
        a = items[0]
    for key in ("command", "name", "brief", "prompt", "style"):
        if isinstance(a.get(key), str) and a[key].strip():
            return a[key].strip()
    return None


def terminal_of(arguments: str | None, tool_name: str) -> str | None:
    """The terminal a shell call types into or looks at; None for other tools."""
    if not tool_name.startswith("shell__"):
        return None
    try:
        a = json.loads(arguments or "")
    except ValueError:
        return None
    return str(a.get("terminal") or "main") if isinstance(a, dict) else None


_SELECT = (
    "SELECT j.id, j.conversation_id, j.tool_name, j.state, j.result, "
    "j.created_at, j.finished_at, j.tool_call_id, j.progress, "
    # the message that brought it its result, once delivered
    # note: scans the job messages per row, an index on a job column if it slows
    "(SELECT d.message_id FROM intercom i, json_each(i.data, '$.jobs') je "
    " JOIN intercom_deliveries d ON d.intercom_id = i.id "
    " WHERE i.kind = 'job' AND je.value = j.id LIMIT 1) AS job_message_id, "
    "j.stopped_by, tc.message_id, tc.started_at, tc.display, tc.arguments "
    "FROM jobs j LEFT JOIN tool_calls tc ON tc.id = j.tool_call_id")


def interrupted_why(tool_name: str) -> str:
    """Why a running job ended at a restart, for its row and its tool message. A shell
    call only watched: a restart of the host takes the
    shellbox down too, so this says only what the app can know."""
    if tool_name.startswith("shell__"):
        return ("the app restarted while watching. shell__read shows whether the terminal "
                "and what ran in it are still there; if the shellbox restarted too, they "
                "are gone.")
    return "the server restarted before it finished. There is no result."


def interrupt_running(c: sqlite3.Connection, store) -> list[tuple[str, str]]:
    """At startup (turns/store.sweep): every job still running ends `interrupted`, its
    detached call too, each said on its conversation's log. -> (conversation, the line
    the restart notice gives it) per job."""
    out = []
    for row in c.execute("SELECT id, conversation_id, tool_name FROM jobs "
                         "WHERE state='running'").fetchall():
        why = interrupted_why(row["tool_name"])
        c.execute("UPDATE jobs SET state='interrupted', result=?, finished_at=? "
                  "WHERE id=?", (why, now(), row["id"]))
        store.event(c, row["conversation_id"], JOB_STATE,
                    {"job_id": row["id"], "state": "interrupted"})
        for call in c.execute(
                "UPDATE tool_calls SET state='interrupted', finished_at=? "
                "WHERE job_id=? AND state='detached' "
                "RETURNING id, name, arguments, result_message_id",
                (now(), row["id"])).fetchall():
            result = turns.content(c, call["result_message_id"])
            store.event(c, row["conversation_id"], TOOL_DONE,
                        {"tool_call_id": call["id"], "name": call["name"], "ok": False,
                         "state": "interrupted", "reason": "the server restarted",
                         "result": result, "display": None,
                         "words": words.words(call["name"], call["arguments"],
                                              "interrupted", None, result)})
        out.append((row["conversation_id"],
                    f"Job {row['id'][:8]} ({row['tool_name']}): {why}"))
    return out


def _view(r: sqlite3.Row) -> dict:
    d = dict(r)
    # while it runs the call has no result display yet
    d["display"] = (json.loads(d["display"])
                    if d["display"] and d["state"] != "running" else None)
    arguments = d.pop("arguments")
    d["prompt"] = prompt_of(arguments, d["tool_name"])
    # a shell job's terminal, also while it runs (display is empty then): the UI
    # opens the live terminal from it
    d["terminal"] = terminal_of(arguments, d["tool_name"])
    return d


async def create(store, job_id: str, conversation_id: str, turn_id: str,
                 tool_call_id: str, tool_name: str) -> None:
    def fn(c: sqlite3.Connection) -> None:
        # the step has run for detach_after_seconds already: its latest line is
        # in the events, and tools only say something when it changes
        last = c.execute(
            "SELECT json_extract(data, '$.note') FROM events WHERE conversation_id=? "
            "AND type='tool.progress' AND json_extract(data, '$.tool_call_id')=? "
            "ORDER BY seq DESC LIMIT 1", (conversation_id, tool_call_id)).fetchone()
        c.execute(
            "INSERT INTO jobs(id,conversation_id,origin_turn_id,tool_call_id,"
            "tool_name,state,created_at,progress) "
            "VALUES(?,?,?,?,?,'running',?,?)",
            (job_id, conversation_id, turn_id, tool_call_id, tool_name, now(),
             last[0] if last else None))
        c.execute("UPDATE tool_calls SET job_id=?, state='detached' WHERE id=?",
                  (job_id, tool_call_id))
        store.event(c, conversation_id, TOOL_DETACHED,
                    {"tool_call_id": tool_call_id, "job_id": job_id, "name": tool_name})
        store.event(c, conversation_id, JOB_STATE,
                    {"job_id": job_id, "state": "running", "tool": tool_name})

    await store.write(fn)


async def finish(store, job_id: str, call: dict, result, call_state: str,
                 job_state: str, stopped_by: str | None = None, wake: bool = True) -> bool:
    """A job ended, in one write: its call gets the verdict (the tool row it read
    stays as written) and the result's display, its row the state, and its
    conversation one `job` message with the result (intercom/deliver.py). -> False when the conversation is gone."""
    from ..intercom.deliver import deliver_idle_sync

    def fn(c: sqlite3.Connection) -> bool:
        ts = now()
        display = json.dumps(result.display) if result.display else None
        row = c.execute(
            "UPDATE tool_calls SET state=?, display=?, "
            "finished_at=? WHERE id=? RETURNING result_message_id, display",
            (call_state, display, ts, call["id"])).fetchone()
        job = c.execute(
            "UPDATE jobs SET state=?, result=?, finished_at=?, "
            "stopped_by=coalesce(?, stopped_by) WHERE id=? "
            "RETURNING conversation_id, tool_name",
            (job_state, result.content[:2000], ts, stopped_by, job_id)).fetchone()
        if job is None:
            return False                  # the conversation is gone
        cid = job["conversation_id"]
        said = turns.content(c, row["result_message_id"]) if row else None
        shown = json.loads(row["display"]) if row and row["display"] else None
        store.event(c, cid, TOOL_DONE,
                    {"tool_call_id": call["id"], "name": call["name"],
                     "ok": call_state == "ok", "state": call_state, "result": said,
                     "display": shown, "job_id": job_id, "finished_at": ts,
                     "words": words.of_call(c, call["id"], call_state, shown, said)})
        store.event(c, cid, JOB_STATE,
                    {"job_id": job_id, "state": job_state, "tool": job["tool_name"]})
        data = {"jobs": [job_id], "tools": [job["tool_name"]]}
        if isinstance((result.display or {}).get("outside"), dict):
            data["outside"] = result.display["outside"]     # guard/mark.py wraps it
        if stopped_by:
            data["stopped_by"] = stopped_by
        intercom_insert(c, f"agent:{cid}", f"agent:{cid}", "job", result.content,
                        data=data, wake=wake)
        deliver_idle_sync(c, store, cid)
        return True

    return await store.write(fn)


async def list(store, state: str | None = None, *,  # noqa: A001 - the module's word for it
               conversation: str | None = None, limit: int = 100,
               after: tuple[int, str, str] | None = None) -> list[dict]:
    """Running first, then the newest. `after`: the (running, created_at, id) of the
    last row of the previous page."""
    def fn(c: sqlite3.Connection) -> list[dict]:
        where: list[str] = []
        args: list = []
        if state:
            where.append("j.state = ?")
            args.append(state)
        if conversation:
            where.append("j.conversation_id = ?")
            args.append(conversation)
        if after is not None:
            r, t, i = after
            where.append("((j.state = 'running') < ? OR ((j.state = 'running') = ? AND "
                         "(j.created_at < ? OR (j.created_at = ? AND j.id < ?))))")
            args += [r, r, t, t, i]
        sql = (_SELECT
               + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY (j.state = 'running') DESC, j.created_at DESC, j.id DESC "
                 "LIMIT ?")
        return [_view(r) for r in c.execute(sql, [*args, limit])]

    return await store.read(fn)


async def get(store, job_id: str) -> dict | None:
    def fn(c: sqlite3.Connection) -> dict | None:
        r = c.execute(f"{_SELECT} WHERE j.id=?", (job_id,)).fetchone()
        return _view(r) if r else None

    return await store.read(fn)
