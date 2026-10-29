"""The agent's mind.

- The agent's own block: the `message_context` hook adds to the end of a new user message, once
  (the cache prefix never changes), its mood, the gap since the last talk, a new model
  and what is down. The time is not in it: the message's own stamp says when it was
  sent and the tail says now (clock.now_line). The block only when something in it
  changed since the last whole block on this branch that it still sees (its report's
  `state` in `message_context`, so it holds across a restart; a compaction or the
  history limit in between makes the next one whole), after a pause here or with an
  unfreeze; else nothing. Its first line is HEAD, as the legend at the top
  of its prompt names it (turns/legend.py). Only in its chats, missions and background
  jobs, and never with automatic context off (`conversations.auto_context`). The agent's notes
  come back in their own block, `[your memory]` (skills/recall.py).
- The agent's mood: the `moods` rows (append-only), each a word of PAD with a strength;
  `current_mood` decays toward its baseline by real hours (her.toml). After each
  finished chat turn the `small` role appraises the exchange; `mind__feel` in that turn
  wins over it.
- The agent's mood on screen: `mind__set_status(text)` sets the 1 to 24 emojis or the kaomoji next
  to its name in the chat head (kv `mood`, GET /api/agent/mood, global event `mood`).
- The agent's thoughts: `mind__think(text)` appends "- HH:MM text" to thoughts/<date>.md in its
  notebook.
"""
from __future__ import annotations

import functools
import json
import logging
import math
import re
import sqlite3
import tomllib
from datetime import datetime, timezone
from typing import Annotated, Literal

from .. import prompts
from ..core.ids import clock, now
from . import ToolContext, ToolResult, guide, tool, described
from .places import offered
from ..clock import zone
from ..core.event_types import MOOD
from ..guard import awareness

log = logging.getLogger("celine.skills.mind")

APPRAISE_SECONDS = 30.0
APPRAISE_FRESH = 300.0     # an appraisal that waited longer (an outage) is dropped
HALF_LIFE_H = 6.0
BASELINE = (0.2, 0.2, 0.3)
GAP_HERE_H, GAP_ANYWHERE_H = 1, 6
GAP_STEPS_H = (12, 24, 48, 96, 168)
HEAD = "[your mind]"       # the block's first line (turns/legend.py)

# emotion word -> (pleasure, arousal, dominance), after ALMA's OCC to PAD table
PAD: dict[str, tuple[float, float, float]] = {
    "jealous": (-0.5, 0.6, 0.3), "hurt": (-0.6, 0.3, -0.5), "angry": (-0.5, 0.6, 0.6),
    "annoyed": (-0.4, 0.3, 0.3), "bored": (-0.3, -0.6, -0.2), "lonely": (-0.5, -0.3, -0.5),
    "anxious": (-0.4, 0.6, -0.5), "embarrassed": (-0.4, 0.4, -0.6),
    "proud": (0.4, 0.3, 0.3), "happy": (0.6, 0.4, 0.3), "excited": (0.6, 0.7, 0.3),
    "affectionate": (0.6, 0.3, 0.1), "grateful": (0.4, 0.2, -0.3),
    "relieved": (0.2, -0.3, 0.4), "curious": (0.3, 0.5, 0.2), "smug": (0.4, 0.1, 0.6),
    "sad": (-0.6, -0.3, -0.4), "guilty": (-0.4, 0.1, -0.5), "content": (0.5, -0.3, 0.2),
}
Emotion = Literal["jealous", "hurt", "angry", "annoyed", "bored", "lonely", "anxious",
                  "embarrassed", "proud", "happy", "excited", "affectionate",
                  "grateful", "relieved", "curious", "smug", "sad", "guilty", "content"]
# the PAD octant a mood is in: signs of (pleasure, arousal, dominance)
OCTANTS = {(1, 1, 1): "lively", (1, 1, 0): "clingy", (1, 0, 1): "relaxed",
           (1, 0, 0): "soft", (0, 1, 1): "hostile", (0, 1, 0): "anxious",
           (0, 0, 1): "cold", (0, 0, 0): "bored"}



# ---- mood ---------------------------------------------------------------------------------

def _at(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(v)


def _toward(m, base, hours: float, half_life_h: float):
    k = 0.5 ** (max(0.0, hours) / half_life_h)
    return tuple(b + (x - b) * k for x, b in zip(m, base))


def current_mood(rows, baseline, half_life_h: float, now_: datetime):
    """The agent's mood at `now_` as a PAD point: from `baseline`, each event (oldest first,
    {at, emotion, strength}) pulls the mood toward its word's point by its strength,
    and between events and up to now the mood decays toward the baseline with
    `half_life_h`."""
    m, last = tuple(baseline), None
    for r in rows:
        at = _at(r["at"])
        if last is not None:
            m = _toward(m, baseline, (at - last).total_seconds() / 3600, half_life_h)
        s = min(1.0, max(0.0, float(r["strength"])))
        m = tuple(x + s * (p - x) for x, p in zip(m, PAD[r["emotion"]]))
        last = at
    if last is not None:
        m = _toward(m, baseline, (_at(now_) - last).total_seconds() / 3600, half_life_h)
    return m


def mood_words(m, baseline) -> tuple[str, str]:
    """(the octant word of `m`, slight, clear or strong by its distance to the baseline)."""
    word = OCTANTS[tuple(int(x >= 0) for x in m)]
    d = math.dist(m, baseline)
    return word, "slight" if d < 0.15 else "clear" if d < 0.4 else "strong"


@functools.cache
def _knobs_of_her() -> tuple[tuple[float, ...], float]:
    """(mood_baseline, mood_half_life_h) from the shipped her.toml (config/her/), read
    once. A broken value is the package's bug: it raises, naming the file."""
    from ..her import BASE
    raw = tomllib.loads((BASE / "her.toml").read_text())
    base = raw.get("mood_baseline", BASELINE)
    half = raw.get("mood_half_life_h", HALF_LIFE_H)
    if not (isinstance(base, (list, tuple)) and len(base) == 3 and all(
            isinstance(x, (int, float)) and -1 <= x <= 1 for x in base)):
        raise ValueError(f"{BASE / 'her.toml'}: mood_baseline is three numbers from -1 to 1")
    if not (isinstance(half, (int, float)) and half > 0):
        raise ValueError(f"{BASE / 'her.toml'}: mood_half_life_h is a number of hours over 0")
    return tuple(float(x) for x in base), float(half)


def _mood_rows(c: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT * FROM (SELECT id, at, emotion, strength, why FROM moods ORDER BY at DESC, "
        "id DESC LIMIT 50) ORDER BY at, id")]


def describe_mood(rows, tz, now_: datetime) -> str:
    """"mood: hostile, strong. annoyed since 21:10: why", "" without any row."""
    if not rows:
        return ""
    base, half = _knobs_of_her()
    word, how = mood_words(current_mood(rows, base, half, now_), base)
    out = f"mood: {word}, {how}."
    for r in reversed(rows):
        at = _at(r["at"])
        if float(r["strength"]) > 0.3 and (now_ - at).total_seconds() < 4 * half * 3600:
            why = " ".join(str(r["why"] or "").split())
            out += f" {r['emotion']} since {clock(at, tz, now_)}" + (f": {why}" if why else "")
            break
    return out


def mood_line(app) -> str:
    """The agent's mood now as one line for its context ("" when it has none yet). Sync: reads
    the database directly."""
    from ..core.store import connect
    c = connect(app.cfg.db_path)
    try:
        c.row_factory = sqlite3.Row
        rows = _mood_rows(c)
    finally:
        c.close()
    return describe_mood(rows, zone(app.cfg), datetime.now(timezone.utc))


async def _mood_now(store, cfg, now_: datetime) -> str:
    return describe_mood(await store.read(_mood_rows), zone(cfg), now_)


async def feel(store, emotion: str, strength: float, why: str, by: str,
               message_id: str | None = None) -> None:
    """One mood row (never deleted)."""
    await store.write(lambda c: c.execute(
        "INSERT INTO moods(at, emotion, strength, why, by, message_id) VALUES(?,?,?,?,?,?)",
        (now(), emotion, min(1.0, max(0.0, float(strength))), why, by,
         message_id)))


async def store_mood(store, text: str) -> dict:
    """The agent's mood on screen, stored (kv `mood`) and sent as the global event `mood`."""
    mood = {"text": text, "set_at": now()}

    def fn(c: sqlite3.Connection) -> None:
        c.execute("INSERT INTO kv(k, v) VALUES('mood', ?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (json.dumps(mood),))
        store.global_event(c, MOOD, {"mood": mood})

    await store.write(fn)
    return mood


async def get_mood(store) -> dict | None:
    raw = await store.kv_get("mood")
    return json.loads(raw) if raw else None


def start(app) -> None:
    """At startup: the appraisal after a chat turn (app.dispatcher.on_end), a work item
    (work.py, `appraisal`)."""
    if "mind" in app.registry.skills:
        app.dispatcher.on_end(lambda cid, turn_id: _ended(app, cid, turn_id))
        app.dispatcher.work.handle("appraisal", ("small",), lambda p: _appraise_item(app, p))


def _ago(seconds: float) -> str:
    h = seconds / 3600
    if h < 48:
        n = max(1, round(h))
        return f"{n} hour{'s' if n != 1 else ''} ago"
    n = round(h / 24)
    return f"{n} days ago"


def _facts(c: sqlite3.Connection, cid: str, tid: str, mid: str, at: str,
           limit: int | None) -> dict:
    """What the block needs from the database, in one read."""
    from .places import place_of
    conv = c.execute("SELECT auto_context FROM conversations WHERE id=?", (cid,)).fetchone()
    # the model of its last reply before this message: up the tree
    model, pid, hops = None, mid, 0
    while pid and hops < 200:
        r = c.execute("SELECT parent_id, role, content, turn_id FROM messages WHERE id=?",
                      (pid,)).fetchone()
        if r is None:
            break
        if r[1] == "assistant" and (r[2] or "").strip():
            t = c.execute("SELECT model FROM turns WHERE id=?", (r[3],)).fetchone()
            model = t[0] if t else None
            break
        pid, hops = r[0], hops + 1
    here = c.execute("SELECT max(created_at) FROM messages WHERE conversation_id=? AND "
                     "id<>? AND created_at<?", (cid, mid, at)).fetchone()[0]
    anywhere = c.execute(
        "SELECT max(m.created_at) FROM messages m JOIN conversations v ON v.id = "
        "m.conversation_id WHERE m.role='user' AND v.kind='chat' AND v.trigger_key IS "
        "NULL AND m.id<>? AND m.created_at<?",
        (mid, at)).fetchone()[0]
    # the state of the last whole block on this branch it still sees (only a whole
    # block's report has one)
    from ..turns.message_context import last_report
    last = last_report(c, cid, mid, "$.mind.state", limit)
    return {"place": place_of(c, cid, tid), "auto": bool(conv[0]) if conv else False,
            "model": model, "here": here, "anywhere": anywhere, "last": last}


def _gap_bucket(hours: float | None) -> int:
    """How long since the last talk anywhere, coarsely: 0 under GAP_ANYWHERE_H, then one
    step at each of GAP_STEPS_H, so the block does not come back every hour."""
    return 0 if hours is None or hours <= GAP_ANYWHERE_H else \
        1 + sum(hours >= h for h in GAP_STEPS_H)


class Mind:
    name = "mind"
    guide = guide("mind")
    description = described()

    def __init__(self, cfg):
        self.cfg = cfg
        self.app = None

    async def prompt_fragment(self, ctx: ToolContext) -> str:
        """The places in its app, as the user sees them (skills/mind_places.md)."""
        return prompts.fill(self.cfg, __file__, "mind_places.md")

    async def message_context(self, ctx: ToolContext, message: dict):
        """The block at the end of a new user message (module docstring): (text, report)
        or None where its mind does not run."""
        cfg, store = self.cfg, ctx.store
        at = message.get("created_at") or now()
        from ..turns.message_context import history_limit
        f = await store.read(lambda c: _facts(c, ctx.conversation_id, ctx.turn_id,
                                              message["id"], at, history_limit(cfg)))
        if not offered("mind", f["place"]) or not f["auto"]:
            return None
        tz, now_ = zone(cfg), _at(at)
        lines = [HEAD]
        mood = await _mood_now(store, cfg, now_)
        if mood:
            lines.append(mood)
        gap = []
        here_h = (now_ - _at(f["here"])).total_seconds() / 3600 if f["here"] else None
        anywhere_h = (now_ - _at(f["anywhere"])).total_seconds() / 3600 \
            if f["anywhere"] else None
        if here_h is not None and here_h > GAP_HERE_H:
            gap.append(f"last message here {_ago(here_h * 3600)}")
        if anywhere_h is not None and anywhere_h > GAP_ANYWHERE_H:
            gap.append(cfg.substitute("last talk with {{username}} anywhere ")
                       + _ago(anywhere_h * 3600))
        if gap:
            lines.append("; ".join(gap))
        model = self._model_now(ctx.conversation_id)
        if model and f["model"] and model != f["model"]:
            lines.append(f"model now: {model}")
        if down := self._down(f["place"]):
            lines.append(f"down now: {', '.join(down)}")
        if unfrozen := await awareness.unfrozen_line(store, cfg, tz):
            lines.append(unfrozen)
        if bastet := await awareness.line(store, cfg, tz, ctx.conversation_id, at):
            lines.append(bastet)
        # the block only when something in it changed since the last whole one it sees
        # (a pause here is always news); else nothing, the report alone
        state = {"mood": mood, "gap": _gap_bucket(anywhere_h), "model": model,
                 "down": down, "bastet": bastet}
        full = state != f["last"] or bool(unfrozen) or (
            here_h is not None and here_h > GAP_HERE_H)
        if not full:
            return "", {"mood": None, "gap": None}
        report = {"mood": mood or None, "gap": "; ".join(gap) or None, "state": state}
        return "\n".join(lines) if len(lines) > 1 else "", report

    def _model_now(self, cid: str) -> str | None:
        llm = getattr(self.app, "llm", None)
        if llm is None:
            return None
        chain = llm.router.chain("chat", cid)
        bc = llm.router.backend_config(chain[0]) if chain else None
        return bc.model if bc is not None else None

    def _down(self, place: str) -> list[str]:
        from ..settings.tools_meta import SKILLS
        from .places import offered
        reg = getattr(self.app, "registry", None)
        if reg is None:
            return []
        out = []
        for name in reg.skills:
            if offered(name, place) and reg.down(name):
                label = SKILLS.get(name, {}).get("label") or name.replace("_", " ")
                out.append(self.cfg.substitute(label))
        return out

    @tool(read_only=False, destructive=False, needs_answer=False)
    async def think(self, ctx: ToolContext,
                    text: Annotated[str, "The thought, in your own words."]) -> ToolResult:
        """Write a thought of yours down: it goes into thoughts/<today>.md in your
        notebook with the time. For you, not an answer. Example:
        mind__think(text="the build keeps failing on the same test")."""
        from . import notebook as nb
        from .notebook import folder_of
        tz = zone(self.cfg)
        local = datetime.now(timezone.utc).astimezone(tz)
        folder = await folder_of(ctx)
        rel = (f"{folder.rstrip('/')}/" if folder else "") + f"thoughts/{local:%Y-%m-%d}.md"
        root = self.cfg.her.notebook
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        line = f"- {local:%H:%M} {' '.join(text.split())}\n"
        old = p.read_text() if p.exists() else ""
        body = nb.stamped(old + ("" if not old or old.endswith("\n") else "\n") + line, self.cfg)
        nb.keep_version(root, p, rel)
        p.write_text(body)
        from . import notebook_index
        await notebook_index.upsert(ctx.store, rel, f"your thoughts on {local:%-d %b %Y}",
                                    len(body), body)
        return ToolResult(content=f"Saved {rel}.", display={"path": rel})

    @tool(read_only=False, destructive=False)
    async def set_status(self, ctx: ToolContext,
                     text: Annotated[str, "Emojis or a kaomoji."]) -> ToolResult:
        """Set the emojis or the kaomoji shown next to your name. Example:
        mind__set_status(text="\U0001f319\u2615\u2728")."""
        mood = await store_mood(ctx.store, text)
        return ToolResult(content=f"Shown: {mood['text']}", display={"text": mood["text"]})

    @tool(read_only=False, destructive=False, needs_answer=False)
    async def feel(self, ctx: ToolContext,
                   emotion: Annotated[Emotion, "How you feel."],
                   strength: Annotated[float, "How strongly, from 0 to 1."],
                   why: Annotated[str, "Why, in a few words."]) -> ToolResult:
        """Say how you feel now. It sets your mood, which fades over hours. Example:
        mind__feel(emotion="curious", strength=0.7, why="{{username}} mentioned a project I want
        to hear more about")."""
        if emotion not in PAD:
            return ToolResult(content=f"{emotion} is not one of the words: "
                                      f"{', '.join(PAD)}.", is_error=True)
        mid = await ctx.store.read(lambda c: (c.execute(
            "SELECT message_id FROM tool_calls WHERE id=?", (ctx.tool_call_id,)).fetchone()
            or [None])[0])
        await feel(ctx.store, emotion, strength, why, "her", mid)
        mood = await _mood_now(ctx.store, self.cfg, datetime.now(timezone.utc))
        return ToolResult(content=f"Noted. {mood}" if mood else "Noted.",
                          display={"emotion": emotion, "strength": strength})


# ---- the appraisal after a chat turn ----------------------------------------------------------


def _turn_facts(c: sqlite3.Connection, cid: str, tid: str) -> dict | None:
    """The exchange of a finished chat turn of its own, or None when it is not one to
    appraise (another place, not done, or it said how it feels)."""
    from .places import place_of
    t = c.execute("SELECT state, root_message_id FROM turns WHERE id=?", (tid,)).fetchone()
    if t is None or t[0] != "done":
        return None
    if place_of(c, cid, tid) != "chat":
        return None
    if c.execute("SELECT 1 FROM tool_calls tc JOIN messages m ON m.id = tc.message_id "
                 "WHERE m.turn_id=? AND tc.name='mind__feel'", (tid,)).fetchone():
        return None
    said = c.execute("SELECT content FROM messages WHERE id=?", (t[1],)).fetchone()
    reply = c.execute("SELECT id, content FROM messages WHERE turn_id=? AND role='assistant' "
                      "AND coalesce(content,'')<>'' ORDER BY created_at DESC, rowid DESC "
                      "LIMIT 1", (tid,)).fetchone()
    if said is None or reply is None:
        return None
    return {"said": said[0] or "", "reply": reply[1], "reply_id": reply[0]}


def appraisal_prompt(cfg, mood: str, said: str, reply: str) -> tuple[str, str]:
    system = prompts.fill(cfg, __file__, "mind_appraisal.md", emotions=", ".join(PAD))
    user = cfg.substitute(f"{{{{name}}}}'s mood before: {mood or 'none yet'}\n\n"
                          f"{{{{username}}}}: {said}\n\n{{{{name}}}}: {reply}")
    return system, user


def parse_appraisal(text: str | None) -> dict | None:
    """{"emotion", "strength", "why"} from the answer, or None (no change, unreadable)."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        got = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(got, dict) or got.get("emotion") not in PAD:
        return None
    s = got.get("strength")
    if isinstance(s, bool) or not isinstance(s, (int, float)):
        return None
    return {"emotion": got["emotion"], "strength": min(1.0, max(0.0, float(s))),
            "why": str(got.get("why") or "")}


async def appraise(app, cid: str, tid: str, backend=None) -> dict | None:
    """One call on the `small` role (background, thinking off, 30 s) after a finished
    chat turn of its own, as a work item (work.py, `appraisal`); the mood row it gives.
    A model error is raised: no host up defers the item, another is a try.
    `backend`: a fixed one (tests)."""
    from ..backends.queue import Priority
    from ..settings.thinking import OFF
    from ..turns.compact import complete
    store, cfg = app.store, app.cfg
    f = await store.read(lambda c: _turn_facts(c, cid, tid))
    if f is None:
        return None
    llm = app.llm
    d = getattr(app, "dispatcher", None)
    if backend is None and d is not None and d.backend_factory is not None:
        backend = d.backend_factory()       # a test: never a real model
    if llm is None or (backend is None and not llm.router.chain("small", cid)):
        log.info("feeling skipped", extra={"event": "mind.appraisal_skipped",
                                           "conversation": cid, "why": "no small model"})
        return None
    small = llm.bind(backend, role="small", priority=Priority.BACKGROUND,
                     conversation_id=cid, lane=f"feeling-{cid}", thinking=OFF, voice=False)
    system, user = appraisal_prompt(cfg, await _mood_now(store, cfg, datetime.now(timezone.utc)),
                                    f["said"], f["reply"])
    text = await complete(small, [{"role": "system", "content": system},
                                  {"role": "user", "content": user}], APPRAISE_SECONDS,
                          errors=True)
    if text is None:
        log.info("feeling skipped", extra={"event": "mind.appraisal_skipped",
                                           "conversation": cid, "turn": tid,
                                           "why": "the small model wrote nothing"})
        return None
    got = parse_appraisal(text)
    if got is not None:
        await feel(store, got["emotion"], got["strength"], got["why"], "appraisal",
                   f["reply_id"])
    return got


async def _ended(app, cid: str, turn_id: str | None) -> None:
    """A chat turn of its own ended: its appraisal goes to the work queue."""
    if turn_id is None or await app.store.read(
            lambda c: _turn_facts(c, cid, turn_id)) is None:
        return
    pool = app.dispatcher.work
    await pool.put("appraisal", {"conversation_id": cid, "turn_id": turn_id,
                                 "at": pool.clock().isoformat()}, conversation_id=cid)


async def _appraise_item(app, p: dict) -> None:
    """The `appraisal` work item. One that waited out an outage is finished without a
    call: its mood now is not that old exchange's."""
    waited = (app.dispatcher.work.clock() - datetime.fromisoformat(p["at"])).total_seconds()
    if waited > APPRAISE_FRESH:
        log.info("feeling skipped", extra={"event": "mind.appraisal_skipped",
                                           "conversation": p["conversation_id"],
                                           "why": f"waited {waited:.0f} s"})
        return
    await appraise(app, p["conversation_id"], p["turn_id"])

