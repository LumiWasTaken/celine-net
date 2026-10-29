"""Events: one list of everything it did and everything that ran, built from the rows that exist, nothing stored twice:
turns, tool calls, its notebook writes (its notebook__* calls), background jobs, the
messages she sent the user, Routine runs, Bastet's decisions, freezes and unfreezes,
passes, the work queue's items (work.py), the changes it made to itself (its talk
and slop list, skills/me.py: their versions in text_versions) and its bug reports
(bugreports/).

Each source is one SELECT with the same first columns (id, at, failed, cid, mid, tid) and
a view that turns its row into an event: {id, at, kind, title, outcome, tone, failed,
day, where, detail, links: {conversation, message, turn, open}}; `day` the list's day head
in the app's zone (Today, Yesterday, the weekday, "9 Oct"). `open` is the app path where
it happened. GET /api/events merges the sources newest first; the global event
`events.added {kind, conversation}` says a new one landed (core/bus.py `lands` for the
conversation events, `added` here for the rest).

`notice` is the other global event here: one item the user hears about while the app's
tab is hidden (a system notification, web/src/lib/notify.ts; a push to a device while no page
is focused, push.py): its finished reply in a
chat, an Intercom message to the user, a mission that waits for them, a failed run, an
approval that waits. {kind, title, body, link, tag}: `link` the app path that opens it,
`tag` the item, so a burst of one item replaces its notification."""
from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from urllib.parse import quote

from fastapi import APIRouter, Request

from . import homes
from .core.ids import iso
from .api.cursor import page_cursor
from .clock import zone
from .core.bus import NOTEBOOK_TOOLS
from .errors import CelineError
from .core.event_types import EVENTS_ADDED, NOTICE

router = APIRouter(prefix="/api")

_NB = "(" + ",".join(f"'{t}'" for t in NOTEBOOK_TOOLS) + ")"
DAYS = 14                 # the days the Date filter offers
LINE = 120                # the most characters of a detail line


def added(store, c: sqlite3.Connection, kind: str, conversation: str | None = None) -> None:
    """Inside a write: a new event of `kind` landed (for the sources no conversation event
    announces: a run, a message to the user)."""
    store.global_event(c, EVENTS_ADDED, {"kind": kind, "conversation": conversation})


def notice(store, c: sqlite3.Connection, kind: str, title: str | None, body: str | None,
           link: str | None, tag: str) -> None:
    """Inside a write: one `notice` (above); `body` cut to its first line."""
    store.global_event(c, NOTICE, {"kind": kind, "title": title or "", "body": _first(body),
                                     "link": link, "tag": tag})


# ── the words ──

def _first(text, n: int = LINE) -> str:
    s = str(text or "").strip().split("\n")[0].strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _loads(text: str | None) -> dict:
    try:
        out = json.loads(text or "{}")
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def _took(start: str | None, end: str | None) -> str:
    """How long from `start` to `end`: "12 s", "3 min", "1 h 5 min"; "" without both."""
    if not start or not end:
        return ""
    s = max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h" + (f" {s % 3600 // 60} min" if s % 3600 >= 60 else "")


def _label(tool: str) -> str:
    from .settings.tools_meta import TOOLS, humanize
    return TOOLS.get(tool, {}).get("label") or humanize(tool)


def _cap(s: str) -> str:
    """The first letter up, the rest as written ("Words aimed at an AI")."""
    return s[:1].upper() + s[1:]


def _join(*parts: str) -> str:
    return ", ".join(p for p in parts if p)


RUNNING, OK, ERROR, MUTED, WARN = "running", "ok", "error", "muted", "warn"
# a state as (word, tone)
_TURN = {"queued": ("Queued", MUTED), "done": ("Done", OK), "aborted": ("Stopped", MUTED), "failed": ("Failed", ERROR),
         "interrupted": ("Failed", ERROR), "capped": ("Paused", WARN)}
_CALL = {"ok": ("Done", OK), "error": ("Failed", ERROR), "gated": ("Stopped", ERROR),
         "interrupted": ("Failed", ERROR), "cancelled": ("Stopped", MUTED)}
_JOB = {"done": ("Done", OK), "failed": ("Failed", ERROR), "timeout": ("Failed", ERROR),
        "interrupted": ("Failed", ERROR), "cancelled": ("Stopped", MUTED)}
_RUN = {"done": ("Done", OK), "failed": ("Failed", ERROR), "skipped": ("Skipped", MUTED),
        "stopped": ("Stopped", MUTED)}
# Bastet's decision as banners.py names it (_kind: Watch's would-be acts too)
_GUARD = {"allow": ("Allowed", OK), "would_stop": ("Would have stopped", WARN),
          "would_freeze": ("Would have frozen", WARN),
          "stop": ("Stopped", ERROR), "freeze": ("Frozen", ERROR), "failed": ("Failed", ERROR),
          "unreachable": ("Could not decide", WARN),
          "found": ("Found", WARN), "closed": ("No answer", ERROR),
          "ask": ("Waiting for you", WARN), "approved": ("Approved", OK),
          "denied": ("Denied", ERROR), "passed": ("Pass", MUTED), "patrol": ("Found", WARN),
          "asked": ("Waiting for you", WARN), "waiting": ("Running", RUNNING)}
# the Mode filter's words: Bastet's mode when she decided
MODE_WORDS = {"on": "On", "watch": "Watch"}
_WORK_STATE = {"queued": ("Queued", MUTED), "deferred": ("Paused", WARN), "done": ("Done", OK),
               "failed": ("Failed", ERROR)}
# its text -> (the title, the key of its page's editor: web/src/lib/nav.tsx)
_HER = {"her:talk": ("Changed the Example lines", "talk"),
        "her:voice": ("Changed the Banned words", "banned"),
        "her:persona": ("Changed the Persona", "persona")}
_BUG = {"high": "High", "medium": "Medium", "low": "Low"}
_WORK = {"title": "Title", "appraisal": "Mood", "documents.describe": "Document descriptions"}


# ── the sources ──

@dataclass(frozen=True)
class Source:
    kind: str
    label: str            # the Type filter's word
    sql: str              # id, at, failed, cid, mid, tid, tab, then its own columns
    view: Callable[[sqlite3.Row, object], dict]   # -> title, outcome, tone, detail, open?


def _model(cfg, ref: str | None, model: str | None) -> str:
    """A model by the name Settings gives it (its id only when Settings no longer has it)."""
    s = getattr(cfg, "settings", None)
    m = s.model(ref) if s is not None and ref else None
    return (m or {}).get("label") or model or ""


def _turn(r, cfg) -> dict:
    word, tone = _TURN.get(r["state"], ("Running", RUNNING))
    return {"title": "Turn", "outcome": word, "tone": tone,
            "detail": _join(_model(cfg, r["backend_id"], r["model"]), _took(r["at"], r["finished_at"]),
                            _first(r["reason"]) if tone in (ERROR, MUTED, WARN) else "")}


def _call(r, cfg) -> dict:
    word, tone = _CALL.get(r["state"], ("Running", RUNNING))
    args = _loads(r["arguments"])
    # the first plain argument says what it was about: a command, a url, a query
    what = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    return {"title": _label(r["name"]), "outcome": word, "tone": tone, "detail": _first(what)}


_NOTE_VERB = {"write": "Wrote", "edit": "Edited", "delete": "Deleted"}


def _note(r, cfg) -> dict:
    word, tone = _CALL.get(r["state"], ("Running", RUNNING))
    args, tool = _loads(r["arguments"]), r["name"].removeprefix("notebook__")
    path = str(args.get("path") or args.get("src") or "")
    title = f"Moved {path} to {args.get('dst') or ''}" if tool == "move" \
        else f"{_NOTE_VERB.get(tool, tool.title())} {path}"
    return {"title": title.strip(), "outcome": word, "tone": tone,
            "detail": _first(args.get("summary")),
            "open": f"/notebook?note={quote(str(args.get('dst') or path))}"
            if tone == OK and tool != "delete" else None}


def _job(r, cfg) -> dict:
    word, tone = _JOB.get(r["state"], ("Running", RUNNING))
    return {"title": _label(r["tool_name"]), "outcome": word, "tone": tone,
            "detail": _first(r["result"]) if tone == ERROR else _took(r["at"], r["finished_at"])}


def _message(r, cfg) -> dict:
    if r["cancelled_at"]:
        word, tone = "Cancelled", MUTED
    elif r["due_at"] and r["due_at"] > iso(_now()):
        word, tone = "Scheduled", RUNNING
    else:
        word, tone = "Sent", OK
    return {"title": f"To {cfg.user}", "outcome": word, "tone": tone,
            "detail": _first(r["text"]), "open": f"/intercom/{r['thread'] or r['iid']}"}


def _run(r, cfg) -> dict:
    from .texts import who
    word, tone = _RUN.get(r["state"], ("Running", RUNNING))
    by = f"Started by {who(cfg, r['started_by'])}" if r["started_by"] != "clock" else ""
    return {"title": r["label"], "outcome": word, "tone": tone,
            "detail": _join(by, _first(r["error"] or r["reason"]) or
                            _took(r["at"], r["finished_at"])),
            "open": homes.run_link(r["night"], r["rid"])}


def _guard(r, cfg) -> dict:
    from .guard import view
    # a closed gate's row is decision closed too: it stopped the call, nobody was asked
    word, tone = ("Stopped", ERROR) if r["rule"] == "gates" else _GUARD.get(
        view.kind(r), ("Running", RUNNING))
    return {"title": _label(r["tool"]), "outcome": word, "tone": tone,
            "detail": _first(r["why"] or view.line(r)),
            "guard": {"id": r["gid"], "actions": view.actions(r)}}


def _freeze(r, cfg) -> dict:
    if r["unfreeze"]:
        return {"title": "Unfreeze", "outcome": "Done", "tone": OK,
                "detail": _took(r["frozen_at"], r["at"])}
    return {"title": "Freeze", "outcome": "Frozen", "tone": ERROR if r["by"] == "guard" else WARN,
            "detail": _first(("Bastet: " if r["by"] == "guard" else "") + r["why"])}


def _pass(r, cfg) -> dict:
    from .guard.rules import label
    until = datetime.fromisoformat(r["until"]).astimezone(zone(cfg))
    return {"title": f"Pass on {label(r['rule'])}", "outcome": "Allowed",
            "tone": OK, "detail": _join(f"until {until:%H:%M}", _first(r["why"]))}


def _work(r, cfg) -> dict:
    word, tone = _WORK_STATE.get(r["state"], ("Running", RUNNING))
    return {"title": _WORK.get(r["kind"], r["kind"]), "outcome": word, "tone": tone,
            "detail": _first(r["error"]) if r["error"] else ""}


def _her(r, cfg) -> dict:
    title, key = _HER.get(r["of"], (r["of"], "persona"))
    return {"title": title, "outcome": "Done", "tone": OK, "detail": _first(r["change"]),
            "open": f"/settings/agent/edit/{key}/history"}


def _bug(r, cfg) -> dict:
    word, tone = ("Closed", OK) if r["state"] == "closed" else \
        ("Open", WARN if r["severity"] == "high" else MUTED)
    # its severity is a word in the cell beside its state; the line under it is its sentence
    sev = _BUG.get(r["severity"], r["severity"])
    return {"title": r["title"], "outcome": word if word == "Closed" else f"{word}, {str(sev).lower()}",
            "tone": tone, "detail": _first(r["what"]),
            "open": f"/intercom/{r['thread']}"}


# its chats' turns and calls are its own; a mission's, a sub-agent's or a background task's
# are background work
_TAB = "CASE cv.kind WHEN 'chat' THEN 'celine' ELSE 'background' END AS tab"
_TOOL_SQL = ("SELECT '{k}:' || t.id AS id, coalesce(t.started_at, m.created_at) AS at, "
             "t.state IN ('error','gated','interrupted') AS failed, m.conversation_id AS cid, "
             "t.message_id AS mid, m.turn_id AS tid, " + _TAB + ", t.name, t.arguments, t.state "
             "FROM tool_calls t JOIN messages m ON m.id = t.message_id "
             "JOIN conversations cv ON cv.id = m.conversation_id "
             "WHERE t.name {op} " + _NB)

# The order is the Type filter's.
SOURCES = {s.kind: s for s in (
    Source("turn", "Turns",
           "SELECT 'turn:' || t.id AS id, t.created_at AS at, "
           "t.state IN ('failed','interrupted') AS failed, t.conversation_id AS cid, "
           "t.root_message_id AS mid, t.id AS tid, " + _TAB + ", t.state, t.reason, t.model, "
           "t.backend_id, t.finished_at FROM turns t "
           "JOIN conversations cv ON cv.id = t.conversation_id", _turn),
    Source("tool", "Tools", _TOOL_SQL.format(k="tool", op="NOT IN"), _call),
    Source("notebook", "Notebook", _TOOL_SQL.format(k="notebook", op="IN"), _note),
    Source("job", "Background jobs",
           "SELECT 'job:' || j.id AS id, j.created_at AS at, "
           "j.state IN ('failed','timeout','interrupted') AS failed, j.conversation_id AS cid, "
           "tc.message_id AS mid, j.origin_turn_id AS tid, 'background' AS tab, j.tool_name, j.state, j.result, "
           "j.finished_at FROM jobs j LEFT JOIN tool_calls tc ON tc.id = j.tool_call_id", _job),
    Source("message", "Intercom",
           "SELECT 'message:' || i.id AS id, i.created_at AS at, 0 AS failed, "
           "substr(i.from_addr, 7) AS cid, NULL AS mid, NULL AS tid, 'celine' AS tab, i.id AS iid, i.thread, "
           "i.text, i.due_at, i.cancelled_at FROM intercom i "
           "WHERE i.from_addr LIKE 'agent:%' AND (', ' || i.to_addr || ', ') LIKE '%, user, %' "
           "AND NOT EXISTS (SELECT 1 FROM bugreports b WHERE b.thread = i.id)",   # Bugs has it
           _message),
    Source("run", "Runs",
           "SELECT 'run:' || r.id AS id, coalesce(r.started_at, r.finished_at, r.created_at) "
           "AS at, r.state = 'failed' AS failed, r.conversation_id AS cid, NULL AS mid, "
           "NULL AS tid, 'background' AS tab, r.id AS rid, r.label, r.night, r.state, r.reason, r.error, "
           "r.finished_at, r.started_by FROM runs r WHERE r.state != 'waiting'", _run),
    Source("guard", "Bastet",
           "SELECT 'guard:' || g.id AS id, g.created_at AS at, "
           "(g.decision IN ('stop','failed','closed','denied') OR g.froze) AS failed, "
           "g.conversation_id AS cid, g.message_id AS mid, g.turn_id AS tid, 'guard' AS tab, "
           "g.id AS gid, "
           "g.tool, "
           "g.decision, g.why, g.does, g.froze, g.mode, g.rule, g.piece, g.source, g.quote, "
           "g.class, g.sink FROM guard_decisions g",
           _guard),
    Source("freeze", "Freeze",
           "SELECT 'freeze:' || f.id AS id, f.at, 0 AS failed, f.conversation_id AS cid, "
           "NULL AS mid, f.turn_id AS tid, 'guard' AS tab, 0 AS unfreeze, f.by, f.why, "
           "NULL AS frozen_at FROM freezes f UNION ALL "
           "SELECT 'unfreeze:' || f.id, f.unfrozen_at, 0, f.conversation_id, NULL, f.turn_id, "
           "'guard', 1, f.unfrozen_by, f.why, f.at FROM freezes f WHERE f.unfrozen_at IS NOT NULL",
           _freeze),
    Source("pass", "Pass",
           "SELECT 'pass:' || p.id AS id, p.created_at AS at, 0 AS failed, "
           "p.conversation_id AS cid, NULL AS mid, NULL AS tid, 'guard' AS tab, p.rule, p.until, p.why "
           "FROM guard_passes p", _pass),
    Source("work", "Titles and moods",
           "SELECT 'work:' || w.id AS id, w.created_at AS at, w.state = 'failed' AS failed, "
           "w.conversation_id AS cid, NULL AS mid, NULL AS tid, 'background' AS tab, w.kind, w.state, w.error, "
           "w.finished_at FROM work w", _work),
    Source("her", "Persona",
           "SELECT 'her:' || v.id AS id, v.created_at AS at, 0 AS failed, NULL AS cid, "
           "NULL AS mid, NULL AS tid, 'celine' AS tab, v.of, v.change FROM text_versions v "
           "WHERE v.of LIKE 'her:%' AND v.by = 'her'", _her),
    Source("bugreport", "Bugs",
           "SELECT 'bugreport:' || b.id AS id, b.created_at AS at, 0 AS failed, "
           "b.conversation_id AS cid, NULL AS mid, NULL AS tid, 'celine' AS tab, b.title, b.severity, b.what, "
           "b.state, b.thread FROM bugreports b", _bug),
)}


# The tabs: (id, word, the sources that land there). A turn, a call or a notebook write
# lands by its conversation (_TAB); the rest by their source.
TABS = (("guard", "Guard", ("guard", "freeze", "pass")),
        ("celine", "Celine", ("turn", "tool", "notebook", "message", "her", "bugreport")),
        ("background", "Background tasks", ("turn", "tool", "notebook", "job", "run", "work")))
TAB_KINDS = {t: kinds for t, _, kinds in TABS}


# ── the list ──

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day_range(day: str, tz) -> tuple[str, str]:
    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise CelineError("bad_request", "day is a date like 2026-01-22", 400) from None
    start = datetime.combine(d, time(), tz)
    return iso(start), iso(datetime.combine(d + timedelta(days=1), time(), tz))


def _before(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        at, eid = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return str(at), str(eid)
    except (ValueError, TypeError):
        raise CelineError("bad_request", "cursor is not one this server gave out", 400) \
            from None


def days(tz, now_: datetime) -> list[dict]:
    """The Date filter: the last DAYS days in the app's zone, as the lists name them."""
    from .conversations.store import group_of
    today = now_.astimezone(tz).date()
    out = []
    for n in range(DAYS):
        d = today - timedelta(days=n)
        noon = datetime.combine(d, time(12), tz).isoformat()
        out.append({"value": d.isoformat(), "label": group_of(0, noon, tz, now_)})
    return out


def _where(c: sqlite3.Connection, cid: str | None, cache: dict) -> tuple[str | None, str | None]:
    """(the conversation's title, the app path that shows it), once per conversation."""
    if not cid:
        return None, None
    if cid not in cache:
        from .intercom.store import source_of
        src = source_of(c, f"agent:{cid}")
        cache[cid] = (src["title"], src["link"]) if src else (None, None)
    return cache[cid]


GUARD = {"decision": "decision = ?", "rule": "rule = ?", "mode": "mode = ?"}
UNDECIDED = "decision = 'unreachable'"   # no judge answered: apart from its stops


def tabs(c: sqlite3.Connection, cfg, day: str | None = None) -> list[dict]:
    """The tabs with every event of `day` (all days without) and the failed ones."""
    where, args = "", []
    if day:
        where, args = " WHERE at >= ? AND at < ?", list(_day_range(day, zone(cfg)))
    n = {t: [0, 0] for t in TAB_KINDS}
    for s in SOURCES.values():
        for tab, total, failed in c.execute(
                f"SELECT tab, count(*), coalesce(sum(failed), 0) FROM ({s.sql}){where} "
                "GROUP BY tab", args):
            n[tab][0] += total
            n[tab][1] += failed
    return [{"id": t, "label": label, "total": n[t][0], "failed": n[t][1]}
            for t, label, _ in TABS]


def listing(c: sqlite3.Connection, cfg, *, kinds: list[str], tab: str | None = None,
            day: str | None = None,
            state: str | None = None, guard: dict | None = None,
            before: tuple[str, str] | None = None, limit: int = 100) -> dict:
    """{events, total, failed, undecided, next}: the events of `kinds` newest first,
    `limit` a page below `before` (at, id). `state` "failed" (what failed) or
    "undecided" (Bastet's decisions she could not make); `guard` {decision, rule, mode}
    narrows to its decisions. `total`, `failed` and `undecided` count every one of the
    day and filters, on the first page only (None below `before`: the client has them
    from its first page)."""
    where, args = [], []
    if tab:
        where.append("tab = ?")
        args.append(tab)
    if day:
        lo, hi = _day_range(day, zone(cfg))
        where.append("at >= ? AND at < ?")
        args += [lo, hi]
    for k, v in (guard or {}).items():
        if v is not None:
            where.append(GUARD[k])
            args.append(v)
    count_where, count_args = list(where), list(args)
    if state == "failed":
        where.append("failed")
    elif state == "undecided":
        where.append(UNDECIDED)
    if before:
        where.append("(at < ? OR (at = ? AND id < ?))")
        args += [before[0], before[0], before[1]]
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    ccond = (" WHERE " + " AND ".join(count_where)) if count_where else ""
    rows, total, n_failed = [], None if before else 0, None if before else 0
    n_undecided = None if before else 0
    # note: each source is scanned and sorted on its own; indexes on the times once
    # a source passes some hundred thousand rows
    for k in kinds:
        s = SOURCES[k]
        rows += [(s, r) for r in c.execute(
            f"SELECT * FROM ({s.sql}){cond} ORDER BY at DESC, id DESC LIMIT ?",
            [*args, limit + 1])]
        if before:
            continue
        t, f = c.execute(f"SELECT count(*), coalesce(sum(failed), 0) FROM ({s.sql}){ccond}",
                         count_args).fetchone()
        total, n_failed = total + t, n_failed + f
        if k == "guard":
            n_undecided = c.execute(
                f"SELECT count(*) FROM ({s.sql})" + (ccond + " AND " if ccond else " WHERE ")
                + UNDECIDED, count_args).fetchone()[0]
    rows.sort(key=lambda x: (x[1]["at"], x[1]["id"]), reverse=True)
    page, cache = rows[:limit], {}
    from .conversations.store import group_of
    tz, now_ = zone(cfg), _now()
    events = []
    for s, r in page:
        title, path = _where(c, r["cid"], cache)
        v = s.view(r, cfg)
        events.append({"id": r["id"], "at": r["at"], "kind": s.kind, "title": v["title"],
                       "outcome": v["outcome"], "tone": v["tone"], "failed": bool(r["failed"]),
                       "day": group_of(0, r["at"], tz, now_),
                       "where": title, "detail": v["detail"], "guard": v.get("guard"),
                       "links": {"conversation": r["cid"], "message": r["mid"], "turn": r["tid"],
                                 "open": v.get("open", path) or path}})
    last = page[-1][1] if page else None
    return {"events": events, "total": total, "failed": n_failed, "undecided": n_undecided,
            "next": page_cursor((last["at"], last["id"])) if len(rows) > limit else None}


def guard_filters(c: sqlite3.Connection) -> dict:
    """Bastet's filters: the decisions, rules and modes her rows hold, in her words."""
    from .guard.rules import label

    def have(col: str) -> list[str]:
        return [r[0] for r in c.execute(
            f"SELECT DISTINCT {col} FROM guard_decisions ORDER BY {col}")]
    return {"decisions": [{"id": d, "label": _GUARD.get(d, (d,))[0]} for d in have("decision")],
            "rules": [{"id": r, "label": _cap(label(r))}
                      for r in have("rule")],
            "modes": [{"id": m, "label": MODE_WORDS.get(m, m)} for m in have("mode")]}


STATES = ("failed", "undecided")


@router.get("/events")
async def list_events(request: Request, tab: str = "celine", kind: str | None = None,
                      day: str | None = None,
                      state: str | None = None, decision: str | None = None,
                      rule: str | None = None, mode: str | None = None,
                      before: str | None = None, limit: int = 100) -> dict:
    """Everything it did and everything that ran, newest first: {events, total, failed,
    undecided, next, tabs, kinds, days, guard}. `tab` one of `tabs`' ids (guard, celine,
    background: TABS), `kind` one of `kinds`' ids (the tab's), `day` a date in
    the app's zone (one of `days`), `state` failed or undecided, `decision`, `rule`,
    `mode` one of `guard`'s (they and undecided list Bastet's rows only), `before` the
    `next` of the page before. A Bastet row's `guard` is {id, actions} (guard/store.py
    one; actions as guard/view.actions), null on any other row."""
    if tab not in TAB_KINDS:
        raise CelineError("bad_request", "tab is one of " + ", ".join(TAB_KINDS), 400)
    if kind is not None and kind not in TAB_KINDS[tab]:
        raise CelineError("bad_request", "kind is one of " + ", ".join(TAB_KINDS[tab]), 400)
    if state is not None and state not in STATES:
        raise CelineError("bad_request", "state is one of " + ", ".join(STATES), 400)
    guard = {"decision": decision, "rule": rule, "mode": mode}
    if state == "undecided" or any(v is not None for v in guard.values()):
        if tab != "guard" or kind not in (None, "guard"):
            raise CelineError("bad_request", "these filters are Bastet's: kind is guard", 400)
        kind = "guard"
    services = request.app.state.services
    cfg, after = services.cfg, _before(before)

    def fn(c: sqlite3.Connection) -> dict:
        out = listing(c, cfg, kinds=[kind] if kind else list(TAB_KINDS[tab]), tab=tab,
                      day=day, state=state, guard=guard, before=after,
                      limit=max(1, min(limit, 500)))
        return {**out, "guard": guard_filters(c),
                "tabs": tabs(c, cfg, day) if after is None else None}
    out = await services.store.read(fn)
    return {**out, "kinds": [{"id": k, "label": SOURCES[k].label} for k in TAB_KINDS[tab]],
            "days": days(zone(cfg), _now())}
