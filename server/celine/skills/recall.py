"""What a new user message brings back from its notebook, graded: `[your memory]`.

Candidates: the notes whose `keys:` line comes up in the talk (the message and the first
turns/loads.KEY_REPLY characters of its last reply before it; loads.key_in: a whole
word or phrase, any case), cut into sections at their `## ` headings (the lines before
the first heading are a section too), at most NOTE_SECTIONS of one note and CANDIDATES in
all, taken from every note alike. Each section is graded by the cosine similarity of
its embedding to the talk's (the Embeddings task, kb/semantic.query_embedder, one call
for the talk and every section). A section comes back only at or over `memory_floor`,
the best first, at most `memory_sections` of them in at most `memory_tokens` tokens
(var/settings/context.toml, turns/loads.read_shares).

Never: a note it carries in full now (an always note, one it or the user loaded, its
note on a loaded skill), a note with `load: off`, one left out here or outside a
sub-agent's folder, nor a section shown on this branch since the last compaction, unless
it changed since. When the embedding model does not answer, the block says so and
nothing comes back: there is no word-only substitute.

The block is added to the end of the message once (turns/message_context.py: stored, sent
byte-identically after) by the notebook skill's message_context hook, where its mind runs
(skills/places.py, the mind skill's places) and never with automatic context off. Its report
(`message_context.report.notebook`): the query, the floor, every candidate section with
its key, score and state (`shown`, `below the floor`, `shown before`, `no room`), and
the error when the embedding model did not answer.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import datetime

HEAD = "[your memory]"
SECTION_CHARS = 2000     # a longer section is cut there, with where the rest is
CANDIDATES = 40          # the most sections graded for one message
NOTE_SECTIONS = 8        # the most of them from one note, so every note gets a chance
EMBED_SECONDS = 10.0
SHOWN = "shown"
BELOW = "below the floor"
BEFORE = "shown before"
NO_ROOM = "no room"
_HEADING = re.compile(r"^## +(.+?)\s*$", re.M)


def sections(text: str) -> list[tuple[str, str]]:
    """A note as (heading, text) sections, cut at its `## ` headings; the lines before the
    first heading are a section with the heading "". Empty sections are left out."""
    cuts = [m.start() for m in _HEADING.finditer(text)]
    out = []
    for a, b in zip([0, *cuts], [*cuts, len(text)]):
        part = text[a:b].strip()
        m = _HEADING.match(part)
        heading = m[1] if m else ""
        if part[m.end():].strip() if m else part:
            out.append((heading, part))
    return out


def _cut(text: str, rel: str) -> str:
    if len(text) <= SECTION_CHARS:
        return text
    at = text.rfind("\n", 0, SECTION_CHARS)
    return text[:at if at > 0 else SECTION_CHARS].rstrip() + \
        f'\nrest via notebook__read(path="{rel}")'


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def _facts(c: sqlite3.Connection, cid: str, tid: str, mid: str, limit: int | None,
           idle: int) -> dict | None:
    """What the block needs from the database, in one read: the conversation's switch,
    folder and left-out notes, the place, its last reply before `mid`, the notes it
    carries now, and the sections shown on this branch since the last
    compaction ({id: hash})."""
    from .places import place_of
    from ..turns import loads
    conv = c.execute("SELECT auto_context, notebook_folder, notes_off FROM conversations "
                     "WHERE id=?", (cid,)).fetchone()
    if conv is None:
        return None
    turn, rows = loads.rows(c, cid)
    now = [(k, n) for (k, n), t in rows.items() if turn - t <= idle]
    from ..turns.message_context import reports
    reply, shown = "", {}
    for role, content, value in reports(c, cid, mid, "$.notebook.shown", limit):
        if role == "assistant" and not reply and content.strip():
            reply = content
        for s in value or ():
            shown.setdefault(s["id"], s["hash"])
    return {"auto": bool(conv[0]), "folder": conv[1],
            "off": set(json.loads(conv[2] or "[]")), "place": place_of(c, cid, tid),
            "reply": reply, "shown": shown,
            "notes": {n for k, n in now if k == "note"}}


def _embed(cfg, query: str, texts: list[str]) -> tuple[list[float], list[list[float]]]:
    """(the query's vector, each text's) in one call to the first model of the
    Embeddings task that answers; raises SemanticError when none does."""
    from ..kb import semantic
    e = semantic.query_embedder(cfg)
    vecs = e.embed([e.s["query_prefix"] + query, *texts], timeout=EMBED_SECONDS)
    return vecs[0], vecs[1:]


async def block(cfg, store, cid: str, tid: str, message: dict
                ) -> tuple[str, dict] | None:
    """(the `[your memory]` block, its report) for the new user message `message`: the
    block empty when no section passed, None when nothing came up (no key in the talk,
    or not here)."""
    from ..kb.embed import SemanticError, cosine
    from . import notebook, notebook_index
    from .places import offered
    from ..turns import loads, tokens
    from ..turns.message_context import history_limit
    s = loads.shares(cfg)
    f = await store.read(lambda c: _facts(c, cid, tid, message["id"], history_limit(cfg),
                                          int(s["idle_turns"])))
    if f is None or not offered("mind", f["place"]) or not f["auto"]:
        return None
    talk = "\n".join([message.get("content") or "", f["reply"][:loads.KEY_REPLY]]).strip()
    root, user = cfg.her.notebook, (getattr(cfg, "user", "") or "").lower()
    carried = f["notes"]
    found: list[dict] = []
    for r in await notebook_index.list(store, f["folder"]):
        rel = r["path"]
        if rel in f["off"] or rel in carried or not notebook.inside(rel, f["folder"]):
            continue
        key = loads.key_in(talk, notebook.keys_of(root, rel))
        text = notebook.read_note(root, rel) if key else None
        if text is None or notebook.load_of(rel, text, user) is not None:   # always or off
            continue
        try:
            date = f"{datetime.fromtimestamp((root / rel).stat().st_mtime):%-d %b %Y}"
        except OSError:
            continue
        for i, (heading, part) in enumerate(sections(cfg.substitute(text))[:NOTE_SECTIONS]):
            sid = f"{rel}#{heading}" if heading else rel
            found.append({"id": sid, "key": key, "date": date, "nth": i,
                          "text": _cut(part, rel), "hash": _hash(part)})
    if not found:
        return None
    # each note's first section, then each one's second, and so on: the cut takes from
    # every note alike (a stable sort keeps the notes' order within one rank)
    found = sorted(found, key=lambda x: x["nth"])[:CANDIDATES]
    report: dict = {"query": talk, "floor": s["memory_floor"],
                    "candidates": [], "shown": []}
    try:
        q, vecs = await asyncio.to_thread(
            _embed, cfg, talk, [f"{x['id']}\n{x['text']}" for x in found])
    except SemanticError as e:
        report["error"] = str(e)
        report["candidates"] = [{"id": x["id"], "key": x["key"]} for x in found]
        return f"{HEAD} not available now: {e}", report
    for x, v in zip(found, vecs):
        x["score"] = round(cosine(q, v), 3)
    counter = tokens.counter(None)
    lines, used = [], 0
    for x in sorted(found, key=lambda x: -x["score"]):
        entry = f"### {x['id']} ({x['score']:.2f}, {x['date']})\n{x['text']}"
        cost = counter.estimate(entry)
        if x["score"] < s["memory_floor"]:
            x["state"] = BELOW
        elif f["shown"].get(x["id"]) == x["hash"]:
            x["state"] = BEFORE
        elif len(lines) >= s["memory_sections"] or used + cost > s["memory_tokens"]:
            x["state"] = NO_ROOM
        else:
            x["state"] = SHOWN
            lines.append(entry)
            used += cost
            report["shown"].append({"id": x["id"], "hash": x["hash"]})
        report["candidates"].append({k: x[k] for k in ("id", "key", "score", "state")})
    report["tokens"] = used
    if not lines:
        return "", report
    return "\n\n".join([f"{HEAD} sections of your notes this message brings up, "
                        "the best match first (similarity from 0 to 1, last changed):",
                        *lines]), report
