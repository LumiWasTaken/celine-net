"""GET /api/search: words found in every place but the Messages archive (GET
/api/archive/search has that one): the agent's chats, Missions, Routines, Notebook,
Documents and the Intercom, each place with its count and its best results. A result is
{title, detail, snippet (escaped HTML with the words found in <mark>), time, link}; the
server builds every link (homes.py for a conversation)."""
from __future__ import annotations

import html
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Query, Request

from . import homes
from .api.deps import in_thread
from .conversations import store as conversations
from .intercom import store as intercom
from .kb import normalize
from .kb.chat import snippet_html
from .kb.documents import match_expr
from .kb.query import parse
from .skills import notebook_index

router = APIRouter(prefix="/api")

PLACES = ("celine", "missions", "routines", "notebook", "documents", "intercom")
LIMIT = 20          # results per place
# ponytail: a place's count stops at what this many matches hold; a COUNT per place if it shows
CAP = 500
HOMES = {"agent": "celine", "missions": "missions", "routines": "routines"}


def _marked(text: str, marks: list[list[int]]) -> str:
    """Plain text and the [start, end) of each match -> escaped HTML with <mark>."""
    out, at = [], 0
    for a, b in marks:
        out += [html.escape(text[at:a], quote=False), "<mark>",
                html.escape(text[a:b], quote=False), "</mark>"]
        at = b
    out.append(html.escape(text[at:], quote=False))
    return "".join(out)


def _empty() -> dict:
    return {"count": 0, "results": []}


def _add(place: dict, result: dict) -> None:
    place["count"] += 1
    if len(place["results"]) < LIMIT:
        place["results"].append(result)


async def _conversations(store, q: str) -> dict[str, dict]:
    """The agent's chats, Missions and Routines: the conversations whose messages hold
    the words, one result each, in the place its home is (homes.of)."""
    hits = await conversations.search(store, q, None, CAP, 24)

    def fn(c) -> dict[str, dict]:
        out = {p: _empty() for p in HOMES.values()}
        seen: set[str] = set()
        for h in hits:
            cid = h["conversation_id"]
            if cid in seen:
                continue
            seen.add(cid)
            where = homes.of(c, cid)
            if (place := HOMES.get(where["home"])) is None:
                continue
            crumbs = homes.path(c, cid)
            _add(out[place], {"title": crumbs[-1]["name"],
                              "detail": crumbs[0]["name"] if len(crumbs) > 1 else None,
                              "snippet": _marked(h["snippet"], h["marks"]),
                              "time": h["created_at"], "link": where["link"]})
        return out

    return await store.read(fn)


async def _notes(store, q: str) -> dict:
    out = _empty()
    for n in await notebook_index.search(store, q, CAP):
        _add(out, {"title": n["path"].removesuffix(".md"), "detail": None,
                   "snippet": snippet_html(n["snippet"]), "time": n["touched_at"],
                   "link": f"/notebook?note={quote(n['path'])}"})
    return out


def _documents(kb, q: str) -> dict:
    _, exact, prefixes = match_expr(q)
    found = kb.search_segments(q, limit=LIMIT)
    out = {"count": found["total_items"], "results": []}
    for f in found["items"]:
        i = f["item"]
        about = i["user_description"] or i["ai_description"] or i["title"] or i["name"]
        out["results"].append({
            "title": i["title"] or i["name"], "detail": i["folder"] or None,
            "snippet": f["best"][0]["snippet"] if f["best"] else
            normalize.highlight(about, exact, prefixes),
            "time": datetime.fromtimestamp(i["updated_at"] / 1000, timezone.utc).isoformat(),
            "link": f"/documents/{i['id']}" + (f"?folder={quote(i['folder'])}" if i["folder"] else "")})
    return out


def _threads(c, q: str) -> dict:
    """The Intercom threads (its All list) with a message that holds every word, its
    newest such message first."""
    _, exact, prefixes = match_expr(q)
    words = normalize.tokens(q)
    out = _empty()
    if not words:
        return out
    has = " AND ".join("instr(lower(text || ' ' || coalesce(subject, '')), ?) > 0" for _ in words)
    # ponytail: a scan of the intercom table per query; an FTS table if it grows large
    rows = c.execute(f"SELECT thread, subject, text, created_at FROM intercom WHERE {has} "
                     "ORDER BY id DESC", words).fetchall()
    if not rows:
        return out
    listed = {t["thread"]: t for t in intercom.threads(c, None, reader="user")}
    seen: set[int] = set()
    for r in rows:
        t = listed.get(r["thread"])
        if t is None or r["thread"] in seen:
            continue
        seen.add(r["thread"])
        _add(out, {"title": t["name"], "detail": r["subject"],
                   "snippet": normalize.highlight(r["text"], exact, prefixes),
                   "time": r["created_at"], "link": f"/intercom/{r['thread']}"})
    return out


@router.get("/search")
async def search(request: Request, q: str = "",
                 places: list[str] = Query(list(PLACES))) -> dict:
    """{places}: each of `places` (default every one, in PLACES order) where the words of
    `q` are found, as {place, count, results}, its best LIMIT results first. A query
    with an archive operator (from:, has:, during: and the others) narrows the Messages
    archive alone, so it finds nothing here."""
    q = q.strip()
    if not q or parse(q).has_filters:
        return {"places": []}
    services = request.app.state.services
    store, want = services.store, [p for p in PLACES if p in places]
    found: dict[str, dict] = {}
    if set(HOMES.values()) & set(want):
        found.update(await _conversations(store, q))
    if "notebook" in want:
        found["notebook"] = await _notes(store, q)
    if "documents" in want and services.kbs is not None \
            and (kb := services.kbs.documents()) is not None:
        found["documents"] = await in_thread(_documents, kb, q)
    if "intercom" in want:
        found["intercom"] = await store.read(lambda c: _threads(c, q))
    return {"places": [{"place": p, **found[p]} for p in want if found.get(p, {}).get("count")]}
