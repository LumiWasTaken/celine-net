"""What a client paints: the snapshot of a conversation, the events after a seq, and
where a running or finished turn stands."""
from __future__ import annotations

import json
import sqlite3

from ..core.states import LIVE, LIVE_SQL, QUEUE_ORDER
from .. import approvals
from ..guard import store as guard_rows
from ..plans import store as plans
from ..skills import words
from ..turns import compactions as comps
from . import samples, tree
from ..core.event_types import (
    LLM_QUEUE,
    MESSAGE_CREATED,
    MESSAGE_DELTA,
    MESSAGE_DONE,
    MESSAGE_REASONING,
    MESSAGE_REOPENED,
    TURN_STATE,
)


async def snapshot(store, cid: str) -> dict:
    """Everything the client needs for a first paint, read in one transaction."""
    def fn(c: sqlite3.Connection) -> dict:
        conv = c.execute(
            "SELECT id, title, pinned, trigger_key, active_leaf_id, trim_below, "
            "next_seq - 1 AS seq, created_at, updated_at, agent_of, agent_name, "
            "access, kind, notebook_folder, archived_at, notes_off "
            "FROM conversations WHERE id=?", (cid,)).fetchone()
        if conv is None:
            raise KeyError(cid)
        mission = c.execute("SELECT name, status, note, until FROM missions WHERE "
                            "conversation_id=?", (cid,)).fetchone()
        from ..intercom.store import source_of
        from ..sub_agents.skill import agents_of
        agents = agents_of(c, cid)
        messages = tree.path(c, cid)
        # the live turns in the order they run (the first is the one running or next);
        # the snapshot's turn is the first, else the newest
        cols = ("t.id, t.state, t.reason, t.reason_code, t.cut, t.continues, "
                "t.root_message_id FROM turns t WHERE t.conversation_id=?")
        queue = c.execute(f"SELECT {cols} AND t.state IN {LIVE_SQL} ORDER BY {QUEUE_ORDER}",
                          (cid,)).fetchall()
        turn = queue[0] if queue else c.execute(
            f"SELECT {cols} ORDER BY t.created_at DESC, t.rowid DESC LIMIT 1",
            (cid,)).fetchone()
        calls: dict[str, list[dict]] = {}
        for m in messages:
            if m["role"] == "assistant":         # "Love this" (conversations/samples.py)
                m["sample"] = c.execute("SELECT 1 FROM kv WHERE k=?",
                                        (samples.KEY + m["id"],)).fetchone() is not None
            rows = c.execute(
                "SELECT tc.id, tc.name, tc.arguments, tc.state, tc.display, "
                "tc.started_at, tc.finished_at, tc.job_id, r.content AS result "
                "FROM tool_calls tc "
                "LEFT JOIN messages r ON r.id = tc.result_message_id "
                "WHERE tc.message_id=? ORDER BY tc.rowid", (m["id"],)).fetchall()
            if rows:
                calls[m["id"]] = [_call(r) for r in rows]
        # a call still at work (here or as a job) shows its latest progress line
        working = {tc["id"]: tc for tcs in calls.values() for tc in tcs
                   if tc["state"] in ("pending", "running", "detached")}
        if working:
            for row in c.execute(
                    "SELECT data FROM events WHERE conversation_id=? AND "
                    "type='tool.progress' ORDER BY seq", (cid,)):
                d = json.loads(row["data"])
                if d.get("tool_call_id") in working:
                    working[d.pop("tool_call_id")]["progress"] = d
            # a job's line outlives the events (they are pruned between turns)
            for tc in working.values():
                if "progress" not in tc and tc["state"] == "detached":
                    r = c.execute("SELECT progress FROM jobs WHERE tool_call_id=? AND "
                                  "state='running'", (tc["id"],)).fetchone()
                    if r and r["progress"]:
                        tc["progress"] = {"note": r["progress"]}
        tdict = None
        if turn is not None:
            tdict = dict(turn)
            tdict["continuable"] = continuable(c, cid, turn)
            if turn["state"] in ("streaming", "preparing", "tools"):
                tdict["live"] = live(c, cid, turn["id"])
        on_path = {m["id"] for m in messages}
        switched: dict[str, dict] = {}
        for row in c.execute(
                "SELECT data FROM events WHERE conversation_id=? AND type='llm.queue' "
                "AND json_extract(data, '$.fallback') = 1 ORDER BY seq",
                (cid,)):
            d = json.loads(row["data"])
            if d.get("message_id") in on_path and d.get("instead_of"):
                switched[d["message_id"]] = {"model": d.get("model"),
                                             "instead_of": d["instead_of"]}
        compactions = [comps.as_view(r) for r in comps.rows(c, cid)
                       if r["through_message_id"] in on_path]
        image_notices = [d for d in (json.loads(r["data"]) for r in c.execute(
            "SELECT data FROM events WHERE conversation_id=? AND type='images.notice' "
            "ORDER BY seq", (cid,))) if d.get("after_message_id") in on_path]
        # Bastet's banners after the messages that made their calls (guard/store.py banners)
        guard = [b for b in guard_rows.banners(c, cid) if b["message_id"] in on_path]
        return {"conversation": {**dict(conv), "notes_off": json.loads(conv["notes_off"]),
                                 "mission": dict(mission) if mission else None,
                                 "agents": agents,
                                 # the app path that shows it (intercom source_of)
                                 "link": (source_of(c, f"agent:{cid}") or {}).get("link")},
                "messages": messages,
                "tool_calls": calls, "compactions": compactions,
                "turn": tdict, "unanswered": tree.unanswered(c, cid), "switched": switched, "image_notices": image_notices,
                "guard": guard, "plan": plans.get_sync(c, cid),
                "approvals": [a for a in approvals.open_in(c, cid)
                              if a["message_id"] in on_path],
                "queue": [{"turn_id": r["id"]} for r in queue],
                "seq": int(conv["seq"])}

    return await store.read(fn)

async def resume(store, cid: str, after_seq: int) -> dict:
    """Snapshot-or-replay, in ONE deferred read transaction."""
    def fn(c: sqlite3.Connection) -> dict:
        conv = c.execute("SELECT trim_below, next_seq - 1 AS seq FROM conversations "
                         "WHERE id=?", (cid,)).fetchone()
        if conv is None:
            raise KeyError(cid)
        if after_seq > 0 and after_seq < int(conv["trim_below"]):
            return {"resync": "history_trimmed"}
        events = [
            {"seq": int(r["seq"]), "type": r["type"], "ts": r["created_at"],
             "data": json.loads(r["data"])}
            for r in c.execute(
                "SELECT seq, type, data, created_at FROM events WHERE conversation_id=? "
                "AND seq > ? ORDER BY seq", (cid, after_seq))
        ]
        return {"events": events, "seq": int(conv["seq"])}

    return await store.read(fn)


def live(c: sqlite3.Connection, cid: str, turn_id: str) -> dict | None:
    """Where a running turn's reply stands, for a first paint mid-turn: the message
    and its phase ("waiting" for a slot, "loading" the model, "thinking" before the
    first token, "flowing", "done"), as the live events would have left it."""
    live: dict | None = None
    mine = False
    for r in c.execute(
            "SELECT type, data FROM events WHERE conversation_id=? AND type IN "
            "('turn.state','message.created','message.reopened','message.delta',"
            "'message.reasoning','message.done','llm.queue') ORDER BY seq",
            (cid,)):
        d = json.loads(r["data"])
        kind = r["type"]
        if kind == TURN_STATE:      # turns queued together: only this one's events
            mine = d.get("turn_id") == turn_id
            continue
        if not mine:
            continue
        if kind == MESSAGE_CREATED:
            if d.get("role") == "assistant":
                live = {"message_id": d["message_id"], "phase": "thinking"}
        elif kind == MESSAGE_REOPENED:
            live = {"message_id": d["message_id"], "phase": "thinking"}
        elif live is None or d.get("message_id") != live["message_id"]:
            continue
        elif kind in (MESSAGE_DELTA, MESSAGE_REASONING):
            live = {"message_id": live["message_id"], "phase": "flowing"}
        elif kind == MESSAGE_DONE:
            live = {"message_id": live["message_id"], "phase": "done"}
        elif kind == LLM_QUEUE and d.get("turn_id") == turn_id:
            state = d.get("state")
            if state == "waiting":
                live = {"message_id": live["message_id"], "phase": "waiting",
                        "ahead": d.get("ahead", 0), "running": d.get("running", 0),
                        "model": d.get("model"), "reason": d.get("reason"),
                        "message": d.get("message")}
            elif state == "loading":
                live = {"message_id": live["message_id"], "phase": "loading",
                        "model": d.get("model")}
            elif state == "running":
                live = {"message_id": live["message_id"], "phase": "thinking"}
    return live


def continuable(c: sqlite3.Connection, cid: str, turn) -> dict:
    """{ok, label, reason}: whether Continue can pick this turn up, the note the
    UI shows under it, and (when not ok) why, in one sentence."""
    state = turn["state"]
    code = turn["reason_code"]
    cut = turn["cut"]
    labels = {"interrupted": "Interrupted.", "aborted": "Stopped.",
              "capped": "Stopped at the step limit.", "failed": "Failed."}
    if state == "failed" and code == "context_too_small":
        labels["failed"] = turn["reason"] or labels["failed"]
    label = "Cut off at the output token limit." if (state == "done" and cut) else \
        labels.get(state, "")

    def no(reason: str) -> dict:
        return {"ok": False, "label": label, "reason": reason}

    if state in LIVE:
        return no("The answer is still being written.")
    latest = c.execute("SELECT id FROM turns WHERE conversation_id=? "
                       "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                       (cid,)).fetchone()
    if latest is None or latest["id"] != turn["id"]:
        return no("A newer turn follows this one.")
    if state == "done" and not cut:
        return no("The answer is complete. Regenerate the answer, or write a new "
                  "message.")
    if state == "failed":
        retryable = code in ("backend_retryable", "crash", "wall_clock", "no_model", "busy")
        # context_too_small: Continue tries again, after you chose a model with
        # more context or gave this one more
        if not retryable and code != "context_too_small":
            return no(turn["reason"] or "This error does not go away by trying again.")
    root = turn["root_message_id"]
    if tree.turn_tail(c, turn["id"], root, tree.leaf(c, cid)) is None:
        return no("You wrote a message or changed the version after this turn.")
    return {"ok": True, "label": label, "reason": None}


def _call(r: sqlite3.Row) -> dict:
    """One tool call as the transcript shows it, its row in words (skills/words.py)."""
    display = json.loads(r["display"]) if r["display"] else None
    return {**dict(r), "display": display,
            "words": words.words(r["name"], r["arguments"], r["state"], display, r["result"])}

