"""What a new user message brings back from its notebook, graded: `[your memory]`.

Candidates are the sections of eligible notes. Two sources feed them: a note whose
`keys:` come up in the talk (the message and the first loads.KEY_REPLY characters of its
last reply before it), and, with Vector Notebook on and its index ready under the same
model, the notes whose stored sections mean something like the talk. Dense is only an
extra source; eligibility, the budgets and the rendering never change with it.

Eligibility is decided before any cut: a note it carries in full now, one with
`load: off`, one left out here, outside a sub-agent's folder, or a root skill note
(`skills/<name>.md`, which comes with skills__load) is never a candidate. Keys stay
guaranteed candidates, so a dense hit that overlaps one keeps its stored score and is not
embedded again; only the fresh key candidates dense did not cover are embedded, in one
batch after the query's one call.

With an answering Embeddings model, candidates are graded by cosine similarity. Keyed
sections use `memory_floor`; unkeyed dense sections use the more conservative
`memory_dense_floor`, since a note without an explicit key needs stronger evidence.
Without one, each keyed note contributes its best lexical section, or its bounded top
excerpt when the talk only names the note. An embedding outage therefore degrades to useful
keyed memory instead of replacing it with an infrastructure error in the prompt.

Never: a note carried in full now, one with `load: off`, one left out here or outside a
sub-agent's folder, nor a section shown on this branch since the last compaction unless it
changed since. Exact duplicate sections are shown once. A dense score is rendered only
against the current chunk whose canonical hash still matches it, so an edited note never
shows new text under an old score. Scores and infrastructure errors belong in the report,
not in the model-facing block.

The block is added to the end of the message once (turns/message_context.py: stored, sent
byte-identically after) by the notebook skill's message_context hook, where its mind runs
(skills/places.py) and never with automatic context off. Its report
(`message_context.report.notebook`): the query, every candidate with its source, key,
score and state, the shown receipts used by later turns, and any embedding error.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import datetime

from .notebook_index import cut as canonical_cut

HEAD = "[your memory]"
SECTION_CHARS = 2000     # a longer chunk is shown with where the rest is
CANDIDATES = 40          # the most sections graded for one message
NOTE_SECTIONS = 8        # the most of them from one note, so every note gets a chance
EMBED_SECONDS = 10.0
SHOWN = "shown"
BELOW = "below the floor"
BEFORE = "shown before"
NO_ROOM = "no room"
DUPLICATE = "duplicate"
SKILL_NOTE = re.compile(r"skills/[^/]+\.md")
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)
_STOP = {"about", "after", "again", "also", "and", "are", "aber", "aus", "bei", "das",
         "dem", "den", "der", "die", "does", "ein", "eine", "einer", "einem", "einen",
         "for", "from", "hab", "habe", "has", "have", "how", "ich", "ist", "mit", "not",
         "oder", "our", "she", "that", "the", "this", "und", "was", "what", "when",
         "where", "which", "with", "your", "you"}


def _display(text: str, rel: str) -> str:
    """A chunk as the block shows it: whole, or cut with where the rest is."""
    if len(text) <= SECTION_CHARS:
        return text
    at = text.rfind("\n", 0, SECTION_CHARS)
    return text[:at if at > 0 else SECTION_CHARS].rstrip() + \
        f'\nrest via notebook__read(path="{rel}")'


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def _visible_reads(c: sqlite3.Connection, cid: str, mid: str) -> set[str]:
    """Notes whose successful notebook__read result is still visible on this branch,
    after its newest compaction and aging mark. Recalling one again is duplication."""
    from ..conversations import tree
    from ..turns import compactions
    chain = tree.path(c, cid)
    pos = {m["id"]: i for i, m in enumerate(chain)}
    if mid in pos:
        chain = chain[:pos[mid] + 1]
        pos = {m["id"]: i for i, m in enumerate(chain)}
    start = max([pos[r["through_message_id"]] + 1 for r in compactions.rows(c, cid)
                 if r["through_message_id"] in pos], default=0)
    st = c.execute("SELECT aged_through FROM context_state WHERE conversation_id=?",
                   (cid,)).fetchone()
    if st and st[0] in pos:
        start = max(start, pos[st[0]] + 1)
    ids = [m["id"] for m in chain[start:] if m["role"] == "assistant"]
    if not ids:
        return set()
    out: set[str] = set()
    for r in c.execute(
            f"SELECT arguments, display FROM tool_calls WHERE name='notebook__read' "
            f"AND state='ok' AND message_id IN ({','.join('?' * len(ids))})", ids):
        args = json.loads(r["arguments"] or "{}")
        display = json.loads(r["display"] or "{}")
        path = display.get("path") or args.get("path")
        if isinstance(path, str):
            out.add(path)
    return out


def _facts(c: sqlite3.Connection, cid: str, tid: str, mid: str) -> dict | None:
    """What the block needs from the database, in one read: the conversation's switch,
    folder and left-out notes, the place, its last reply before `mid`, and the sections
    shown on this branch since the last compaction ({id: hash})."""
    from .places import place_of
    conv = c.execute("SELECT auto_context, notebook_folder, notes_off FROM conversations "
                     "WHERE id=?", (cid,)).fetchone()
    if conv is None:
        return None
    from ..turns.message_context import reports
    reply, shown = "", {}
    for role, content, value in reports(c, cid, mid, "$.notebook.shown"):
        if role == "assistant" and not reply and content.strip():
            reply = content
        for s in value or ():
            shown.setdefault(s["id"], s["hash"])
    return {"auto": bool(conv[0]), "folder": conv[1],
            "off": set(json.loads(conv[2] or "[]")), "place": place_of(c, cid, tid),
            "reply": reply, "shown": shown, "visible": _visible_reads(c, cid, mid)}


def _embed(cfg, query: str, texts: list[str]) -> tuple[list[float], list[list[float]]]:
    """(the query's vector, each text's) in one call to the first model of the
    Embeddings task that answers; raises SemanticError when none does. The baseline
    keyed grading, used when Vector Notebook is off or its index is not ready."""
    from ..kb import semantic
    e = semantic.query_embedder(cfg)
    vecs = e.embed([e.s["query_prefix"] + query, *(e.s["text_prefix"] + t for t in texts)],
                   timeout=EMBED_SECONDS)
    return vecs[0], vecs[1:]


def _date(root, rel: str) -> str | None:
    try:
        return f"{datetime.fromtimestamp((root / rel).stat().st_mtime):%-d %b %Y}"
    except OSError:
        return None


def _words(text: str) -> set[str]:
    return {w.casefold() for w in _WORD.findall(text)
            if len(w) > 1 and w.casefold() not in _STOP}


def _lexical(candidates: list[dict], talk: str) -> list[dict]:
    """One bounded candidate per keyed note: the section sharing the most non-key words
    with the talk, else its first section. No corpus-wide keyword memory injection."""
    best: dict[str, dict] = {}
    for x in candidates:
        terms = _words(talk) - _words(x["key"] or "")
        hits = len(terms & _words(x["embed"]))
        if x["path"] not in best or hits > best[x["path"]]["lexical_hits"]:
            best[x["path"]] = {**x, "lexical_hits": hits}
    out = list(best.values())
    for x in out:
        x["source"] = "key lexical"
        x["score"] = None
    return out


def _rank(found: list[dict], lexical: bool, diverse: bool = False) -> list[dict]:
    """The baseline's global score order, or, with dense candidates, the best section per
    note first before the remaining useful passes. Lexical fallback uses hits then order."""
    if lexical:
        return sorted(found, key=lambda x: (-x.get("lexical_hits", 0), x["order"]))
    if not diverse:
        return sorted(found, key=lambda x: (x["score"] is None, -(x["score"] or 0),
                                           x["order"]))
    groups: dict[str, list[dict]] = {}
    seq: list[str] = []
    for x in found:
        if x["path"] not in groups:
            groups[x["path"]] = []
            seq.append(x["path"])
        groups[x["path"]].append(x)
    for g in groups.values():
        g.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0), x["order"]))
    at = {p: i for i, p in enumerate(seq)}
    order = sorted(seq, key=lambda p: (groups[p][0]["score"] is None,
                                       -(groups[p][0]["score"] or 0), at[p]))
    out: list[dict] = []
    depth = 0
    while any(depth < len(groups[p]) for p in order):
        for p in order:
            if depth < len(groups[p]):
                out.append(groups[p][depth])
        depth += 1
    return out


def _hydrate(root, cfg, x: dict, cache: dict) -> bool:
    """The current chunk's text for a stored score, only when its canonical hash still
    matches: never renders new text under an old score."""
    from . import notebook, notebook_index
    if x["path"] not in cache:
        text = notebook.read_note(root, x["path"])
        cache[x["path"]] = (notebook_index.cut(text, x["path"], x["summary"],
                                               cfg.substitute) if text is not None else [])
    for row in cache[x["path"]]:
        if row["nth"] == x["nth"] and row["chunk"] == x["chunk"]:
            if row["hash"] != x["hash"]:
                return False
            x["text"] = _display(row["text"], x["path"])
            x["content"] = _hash(row["text"])
            x["embed"] = row["payload"]
            return True
    return False


async def _dense(cfg, store, talk: str, eligible: dict[str, dict], keyed: list[dict],
                 report: dict) -> list[dict] | None:
    """The candidates for a message with Vector Notebook on: the query embedded once, the
    stored chunks scored (their scores copied onto the keyed candidates they cover), the
    dense-only hits hydrated only where their hash still matches, and the genuinely fresh
    keyed candidates embedded in one batch. None on any embedding failure, so the caller
    falls back to the keyed lexical memory."""
    from ..kb.embed import SemanticError
    from . import notebook_semantic
    try:
        qv = await asyncio.to_thread(notebook_semantic.query_vector, cfg, talk)
    except SemanticError as e:
        report["error"] = str(e)
        return None
    rows = [r for r in await store.read(lambda c: notebook_semantic.dense(c, qv))
            if r["path"] in eligible]
    by_ident = {(x["path"], x["nth"], x["chunk"]): x for x in keyed}
    dense_only: list[dict] = []
    for r in rows:
        x = by_ident.get((r["path"], r["nth"], r["chunk"]))
        if x is not None:
            if x["score"] is None and x["hash"] == r["hash"]:
                x["score"] = r["score"]        # covered: its stored score, not embedded
                x["source"] = "key+dense"
            continue
        info = eligible[r["path"]]
        dense_only.append({
            "id": f'{r["path"]}#{r["heading"]}' if r["heading"] else r["path"],
            "path": r["path"], "nth": r["nth"], "chunk": r["chunk"],
            "heading": r["heading"], "key": None, "date": info["date"],
            "summary": info["summary"], "embed": None, "text": None, "hash": r["hash"],
            "content": None, "score": r["score"], "source": "dense",
            "order": len(keyed) + len(dense_only)})
    root = cfg.agent.notebook
    cache: dict[str, list] = {}
    counts: dict[str, int] = {}
    capped: list[dict] = []
    slots = max(0, CANDIDATES - len(keyed))    # keys stay guaranteed, one total cap
    for x in dense_only:                       # best sections per note, capped per note
        if len(capped) >= slots:
            break
        if counts.get(x["path"], 0) >= NOTE_SECTIONS:
            continue
        if _hydrate(root, cfg, x, cache):
            counts[x["path"]] = counts.get(x["path"], 0) + 1
            capped.append(x)
    uncovered = [x for x in keyed if x.get("score") is None]
    if uncovered:
        try:
            vecs = await asyncio.to_thread(notebook_semantic.embed_texts, cfg,
                                           [x["embed"] for x in uncovered])
        except SemanticError as e:
            report["error"] = str(e)
            return None
        if any(v.fp != qv.fp for v in vecs):
            report["error"] = "the embedding endpoint changed during recall"
            return None
        for x, v in zip(uncovered, vecs):
            x["score"] = round(notebook_semantic.score(qv, v), 3)
    return keyed + capped


async def block(cfg, store, cid: str, tid: str, message: dict
                ) -> tuple[str, dict] | None:
    """(the `[your memory]` block, its report) for the new user message `message`:
    None when no eligible note is a candidate, an empty block when every candidate was
    below the floor or shown before."""
    from ..kb.embed import SemanticError, cosine
    from ..settings import features
    from . import notebook, notebook_index
    from .places import offered
    from ..turns import loads, tokens
    s = loads.shares(cfg)
    f = await store.read(lambda c: _facts(c, cid, tid, message["id"]))
    if f is None or not offered("mind", f["place"]) or not f["auto"]:
        return None
    talk = "\n".join([message.get("content") or "", f["reply"][:loads.KEY_REPLY]]).strip()
    root, user = cfg.agent.notebook, (getattr(cfg, "user", "") or "").lower()
    on = features.on(cfg, "vector_notebook")

    # eligibility before any key or dense cut
    eligible: dict[str, dict] = {}
    for r in await notebook_index.list(store, f["folder"]):
        rel = r["path"]
        if rel in f["off"] or rel in f["visible"] or SKILL_NOTE.fullmatch(rel) or \
                not notebook.inside(rel, f["folder"]):
            continue
        head = notebook.head_of(root, rel)
        if head is None or notebook.load_of(rel, head, user) is not None:
            continue
        date = _date(root, rel)
        if date is not None:
            eligible[rel] = {"summary": r["summary"], "date": date}

    keyed: list[dict] = []
    for rel in sorted(eligible):
        key = loads.key_in(talk, notebook.recall_keys(root, rel, user))
        if not key:
            continue
        text = notebook.read_note(root, rel)
        if text is None:
            continue
        for row in canonical_cut(text, rel, eligible[rel]["summary"], cfg.substitute):
            if row["nth"] >= NOTE_SECTIONS:
                continue
            keyed.append({"id": row["sid"], "path": rel, "nth": row["nth"],
                          "chunk": row["chunk"], "heading": row["heading"], "key": key,
                          "date": eligible[rel]["date"], "embed": row["payload"],
                          "raw": row["text"][:SECTION_CHARS],
                          "text": _display(row["text"], rel), "hash": row["hash"],
                          "content": _hash(row["text"]), "score": None,
                          "source": "key", "order": len(keyed)})
    # each note's first section, then each one's second, and so on: every keyed note gets
    # a chance before anything is embedded, and dense gets only the slots that remain
    keyed = sorted(keyed, key=lambda x: (x["nth"], x["order"]))[:CANDIDATES]
    for i, x in enumerate(keyed):
        x["order"] = i

    report: dict = {"query": talk, "floor": s["memory_floor"],
                    "dense_floor": s["memory_dense_floor"], "candidates": [], "shown": []}
    lexical = False
    dense_ready = False
    if on:
        from . import notebook_semantic
        dense_ready = await store.read(notebook_semantic.ready_of)
    if dense_ready:
        found = await _dense(cfg, store, talk, eligible, keyed, report)
        if found is None:
            found = _lexical(keyed, talk)
            lexical = True
    else:
        found = keyed
        if found:
            try:
                q, vecs = await asyncio.to_thread(
                    _embed, cfg, talk, [f"{x['id']}\n{x['raw']}" for x in found])
                for x, v in zip(found, vecs):
                    x["score"] = round(cosine(q, v), 3)
            except SemanticError as e:
                report["error"] = str(e)
                found = _lexical(found, talk)
                lexical = True
    if not found:
        return None
    found = _rank(found, lexical, dense_ready)[:CANDIDATES]
    for x in found:
        x.setdefault("score", None)

    counter = tokens.counter(None)
    lines, used = [], 0
    content_seen: set[str] = set()
    for x in found:
        content = x.get("content") or _hash(x["text"] or "")
        floor = s["memory_dense_floor"] if x["source"] == "dense" else s["memory_floor"]
        if not lexical and (x["score"] is None or x["score"] < floor):
            x["state"] = BELOW
        else:
            if content in content_seen:
                x["state"] = DUPLICATE
            elif f["shown"].get(x["id"]) == x["hash"]:
                x["state"] = BEFORE
            else:
                entry = f"### {x['id']} ({x['date']})\n{x['text']}"
                cost = counter.estimate(entry)
                if len(lines) >= s["memory_sections"] or used + cost > s["memory_tokens"]:
                    x["state"] = NO_ROOM
                else:
                    x["state"] = SHOWN
                    lines.append(entry)
                    used += cost
                    report["shown"].append({"id": x["id"], "hash": x["hash"]})
            content_seen.add(content)
        report["candidates"].append({k: x.get(k) for k in
                                     ("id", "key", "source", "score", "state")})
    report["tokens"] = used
    if lexical:
        report["fallback"] = "key lexical"
    if not report["candidates"]:
        return None
    if not lines:
        return "", report
    return "\n\n".join([f"{HEAD} sections of your notes this message brings up, "
                        "the best match first (last changed):",
                        *lines]), report
