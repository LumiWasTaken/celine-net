"""What a schedule's prompt can hold beyond the night's dates (schedules.fill), filled
when its run starts, each only when the prompt names it:

- {{mood}}: its mood now (skills/mind.mood_line);
- {{on_mind}}: the notes whose `keys:` come up in its last thoughts and its last
  exchange with {{username}} (turns/loads.key_in), each with its summary;
- {{tool_failures}}: per tool, the calls that failed that night: how many, the first
  error, the conversations (a digest instead of every transcript);
- {{voice_counts}}: its chat replies of the last 7 days against {{username}}'s
  messages: slop hits per 1,000 characters (turns/slop.find), the phrases it uses far
  more (turns/slop.profile), the share of replies that start
  lowercase, and how long it answers after {{username}} asked for short.

A value that has nothing to say is "(none)" (schedules.NONE)."""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from ..core.ids import iso
from . import NONE, night_start

DAYS = 7                       # the voice counts' window
TOP = 20                       # phrases it uses far more than the user
ERROR_CHARS = 200              # of the first error text
THOUGHTS = 5                   # its last thoughts {{on_mind}} looks in
ON_MIND = 8                    # the most notes {{on_mind}} names
SHORT = re.compile(r"\b(short|shorter|brief|briefly|tl;?dr|kurz)\b", re.I)
# a message the user wrote: typed here, or sent from the composer through the intercom
FROM_USER = ("m.role = 'user' AND (m.author = 'user' OR m.intercom_id IN "
             "(SELECT id FROM intercom WHERE from_addr = 'user'))")


async def extras(app, text: str, night: date,
                 now: datetime | None = None) -> dict[str, str]:
    """The values of the variables `text` names, for a run of `night`."""
    from ..clock import zone
    now = now or datetime.now(timezone.utc)
    store, cfg = app.store, app.cfg
    out: dict[str, str] = {}
    if "{{mood}}" in text:
        from ..skills import mind
        out["mood"] = mind.mood_line(app) or NONE
    if "{{on_mind}}" in text:
        out["on_mind"] = await on_mind(app)
    if "{{tool_failures}}" in text:
        out["tool_failures"] = await store.read(
            lambda c: tool_failures(c, night, zone(cfg)))
    if "{{voice_counts}}" in text:
        voice = cfg.her.voice if getattr(cfg, "her", None) else {}
        out["voice_counts"] = await store.read(lambda c: voice_counts(c, now, voice))
    return out


# ---- on_mind ----------------------------------------------------------------------------

def last_thoughts(root: Path | None, n: int = THOUGHTS) -> list[str]:
    """The agent's last `n` thought lines, from the newest thoughts/<date>.md notes."""
    if root is None or not (root / "thoughts").is_dir():
        return []
    lines: list[str] = []
    for p in sorted((root / "thoughts").glob("*.md"), reverse=True):
        lines = [ln for ln in p.read_text(errors="replace").splitlines()
                 if ln.startswith("- ")] + lines
        if len(lines) >= n:
            break
    return lines[-n:]


def last_exchange(c: sqlite3.Connection) -> list[str]:
    """The newest message the user wrote it in a chat, and its newest reply there; a
    locked chat (conversations/store.locked) is left out."""
    from ..conversations.store import locked_sql
    out = []
    for where in (FROM_USER, "m.role = 'assistant' AND m.author = 'model'"):
        r = c.execute(
            "SELECT m.content FROM messages m JOIN conversations c ON c.id = "
            f"m.conversation_id WHERE c.kind = 'chat' AND {where} AND "
            f"m.content != '' AND NOT {locked_sql()} "
            "ORDER BY m.created_at DESC LIMIT 1").fetchone()
        if r:
            out.append(r[0][:600])
    return out


async def on_mind(app) -> str:
    """The notes its last thoughts and its last exchange name a key of, the most
    recently touched ON_MIND."""
    from ..skills import notebook_index
    from ..skills.notebook import keys_of
    from ..turns.loads import key_in
    root = app.cfg.her.notebook if getattr(app.cfg, "her", None) else None
    said = "\n".join([*last_thoughts(root), *await app.store.read(last_exchange)])
    if root is None or not said.strip():
        return NONE
    hits = [r for r in await notebook_index.list(app.store, order="touched")
            if key_in(said, keys_of(root, r["path"]))]
    return "\n".join(f"- {r['path']}: {r['summary']}" for r in hits[:ON_MIND]) or NONE


# ---- tool_failures ---------------------------------------------------------------------

def tool_failures(c: sqlite3.Connection, night: date, tz) -> str:
    """The failed calls of `night` (12:00 the day before to 12:00 on its date), one
    line per tool, the most failures first; none of a locked conversation
    (conversations/store.locked)."""
    from ..conversations.store import locked_sql
    lo = night_start(night, tz)
    rows = c.execute(
        "SELECT tc.name, r.content AS error, coalesce(nullif(cv.title, ''), 'Untitled') "
        "AS title FROM tool_calls tc JOIN messages m ON m.id = tc.message_id "
        "JOIN conversations cv ON cv.id = m.conversation_id "
        "LEFT JOIN messages r ON r.id = tc.result_message_id "
        "WHERE tc.state = 'error' AND tc.started_at >= ? AND tc.started_at < ? "
        f"AND NOT {locked_sql('cv')} ORDER BY tc.started_at", (iso(lo), iso(lo + timedelta(days=1)))).fetchall()
    tools: dict[str, dict] = defaultdict(lambda: {"n": 0, "error": "", "in": {}})
    for r in rows:
        t = tools[r["name"]]
        t["n"] += 1
        t["error"] = t["error"] or " ".join((r["error"] or "").split())[:ERROR_CHARS]
        t["in"][r["title"]] = True
    if not tools:
        return NONE
    return "\n".join(
        f"- {name}: {t['n']} failed. First error: {t['error'] or '(no text)'} "
        f"In: {', '.join(t['in'])}."
        for name, t in sorted(tools.items(), key=lambda x: -x[1]["n"]))


# ---- voice_counts ------------------------------------------------------------------------

def _texts(c: sqlite3.Connection, since: str) -> tuple[list[dict], list[dict]]:
    """(its chat replies, the user's messages there), each {cid, at, text}, oldest
    first."""
    def rows(where: str) -> list[dict]:
        return [dict(r) for r in c.execute(
            "SELECT m.conversation_id AS cid, m.created_at AS at, m.content AS text FROM "
            "messages m JOIN conversations c ON c.id = m.conversation_id WHERE c.kind = "
            f"'chat' AND {where} AND m.content != '' AND "
            "m.created_at >= ? ORDER BY m.created_at", (since,))]
    return rows("m.role = 'assistant' AND m.author = 'model'"), rows(FROM_USER)


def _per_k(texts: list[str], voice: dict) -> float:
    from ..turns import slop
    chars = sum(len(t) for t in texts)
    return 1000 * sum(len(slop.find(t, voice)) for t in texts) / chars if chars else 0.0


def voice_counts(c: sqlite3.Connection, now: datetime, voice: dict) -> str:
    from ..turns import slop
    hers, theirs = _texts(c, iso(now - timedelta(days=DAYS)))
    if not hers:
        return NONE
    h, u = [m["text"] for m in hers], [m["text"] for m in theirs]
    lines = [f"Your chat replies of the last {DAYS} days: {len(h)} ({sum(map(len, h)):,} "
             f"characters). {{{{username}}}}'s messages: {len(u)}.",
             f"Slop hits per 1,000 characters: yours {_per_k(h, voice):.1f}, "
             f"{{{{username}}}}'s {_per_k(u, voice):.1f}."]
    lettered = [t for t in h if re.search(r"[^\W\d_]", t)]
    low = sum(1 for t in lettered if re.search(r"[^\W\d_]", t)[0].islower())
    lines.append(f"Replies that start lowercase: {round(100 * low / len(lettered))}%."
                 if lettered else "Replies that start lowercase: (none).")
    after = []
    for m in theirs:
        if SHORT.search(m["text"]):
            nxt = next((r for r in hers if r["cid"] == m["cid"] and r["at"] > m["at"]), None)
            if nxt:
                after.append(len(nxt["text"].split()))
    lines.append(f"After {{{{username}}}} asked for short, your reply was "
                 f"{median(after):.0f} words (median of {len(after)})." if after else
                 "{{username}} did not ask for short.")
    top = list(slop.profile(h, u))[:TOP]
    lines.append("What you write far more than {{username}}: " + (", ".join(
        f'"{p[0]}" ({p[1]:.1f} times)' for p in top) if top else "(none)") + ".")
    return "\n".join(lines)
