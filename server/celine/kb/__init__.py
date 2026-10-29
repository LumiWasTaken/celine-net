"""Knowledge bases: config/kb.toml -> one module instance per block.

A knowledge base is anything with an `id`, `kind`, `title` and `info()`.
`chat` and `documents` are the kinds today; a new kind is a new module and one line
in KINDS. Instant search fans out to every chat archive and merges; nothing joins
across files. Documents have their own search (kb/documents.py).
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from . import instant
from .chat import ChatKB
from .documents import DocumentsKB
from .identity import REF, Identities, group_counts
from .query import understand, warm
from .tasks import Tasks

log = logging.getLogger("celine.kb")

KINDS = ("chat", "documents")


def documents_root(cfg, block: dict[str, Any] | None = None) -> Path | None:
    """The Documents folder of a kind = "documents" block (the first one by default),
    None without one. Relative to the data directory, so a second instance
    (CELINE_DATA_DIR) gets its own folder; an absolute path (an Obsidian vault) is used
    as is."""
    if block is None:
        block = next((b for b in cfg.kb.values() if b.get("kind") == "documents"), None)
        if block is None:
            return None
    root = Path(str(block.get("path", "documents"))).expanduser()
    return root if root.is_absolute() else cfg.data_dir / root

class KBs:
    def __init__(self, cfg, publish, llm=None) -> None:
        self.cfg = cfg
        self.llm = llm          # the app's LLM layer, for descriptions and captions
        self.dir: Path = cfg.data_dir / "kb"     # made by the first file's migrate
        self.all: dict[str, ChatKB | DocumentsKB] = {}
        self.tasks = Tasks(publish)
        from ..settings import features
        for kid in cfg.kb:
            if features.allows_connector(cfg, kid):        # Settings, Features: off
                self.open(kid)
        # name caches and dateparser, so the first from:/in:/date query is not slow
        threading.Thread(target=warm, args=(list(self.all.values()),),
                         daemon=True, name="instant-warm").start()

    def _build(self, kid: str, block: dict[str, Any]):
        kind = block.get("kind")
        title = str(block.get("title") or kid)
        if kind == "chat":
            return ChatKB(id=kid, title=title, platform=str(block.get("platform", kid)),
                          path=self.dir / f"{kid}.db", options=dict(block))
        if kind == "documents":
            return DocumentsKB(id=kid, title=title, root=documents_root(self.cfg, block),
                               path=self.dir / f"{kid}.db",
                               options=dict(block), app_cfg=self.cfg, tasks=self.tasks,
                               llm=self.llm)
        raise ValueError(f"Unknown kind {kind!r}. Known kinds: {', '.join(KINDS)}.")

    def open(self, kid: str) -> None:
        """A source switched on (its feature's runner): its archive, if not open."""
        if kid in self.all or kid not in self.cfg.kb:
            return
        try:
            self.all[kid] = self._build(kid, self.cfg.kb[kid])
        except Exception as e:
            self.cfg.problems.append(_problem(f"[{kid}] {e}"))
            log.warning("knowledge base failed to open",
                        extra={"event": "kb.open_failed", "kb": kid, "error": str(e)})

    def close(self, kid: str) -> None:
        """A source switched off: its archive gone from the lists, its file kept."""
        kb = self.all.pop(kid, None)
        if kb is not None and hasattr(kb, "close"):
            kb.close()

    # ---- lookup ----------------------------------------------------------------------
    def get(self, kid: str) -> ChatKB | DocumentsKB | None:
        return self.all.get(kid)

    def chats(self) -> list[ChatKB]:
        return [k for k in self.all.values() if isinstance(k, ChatKB)]

    def documents(self) -> DocumentsKB | None:
        return next((k for k in self.all.values() if isinstance(k, DocumentsKB)), None)

    def citable(self) -> set[str]:
        """Knowledge base ids whose `[[id:n]]` citations the verifier checks: every chat
        archive (n = message id) and Documents (n = segment id)."""
        return {k.id for k in self.all.values() if isinstance(k, (ChatKB, DocumentsKB))}

    def pick(self, ids: str | list[str] | None) -> list[ChatKB | DocumentsKB]:
        if not ids:
            return list(self.all.values())
        if isinstance(ids, str):
            ids = [x.strip() for x in ids.split(",") if x.strip()]
        return [self.all[i] for i in ids if i in self.all]

    # ---- search ----------------------------------------------------------------------
    def search(self, q: str, *, kbs: str | list[str] | None = None,
               chat: str | None = None, person: str | None = None,
               ts_from: int | None = None, ts_to: int | None = None,
               sort: str = "recent", limit: int = 50, cursor: str | None = None,
               count_cap: int | None = None, top: int = 0, correct: bool = True,
               identities: Identities | None = None) -> dict[str, Any]:
        """Instant search over the messages of every chat archive picked. `q` takes the
        operators of kb/query.py (from: in: kb: before: after: during: has: is:).
        `chat` is a chat reference ("whatsapp:12") and narrows the search to that
        knowledge base. `person` is an account reference: it covers every account of the
        same person (`identities`), which also resolves `from:` names and aliases.
        `sort`: recent | oldest | best | relevance. Paging: `next_cursor` of one
        response as `cursor` of the next.
        `count_cap`: `total` stops counting there (`total_capped`), None = exact.
        `top`: that many `top_hits` (best) next to a recent/oldest first page."""
        t0 = time.perf_counter()
        ids = identities or Identities()
        chat_ref = _split(chat)
        senders = _senders(person, ids)
        targets = self._targets(kbs, chat_ref, senders)
        chat_kbs = [k for k in targets if isinstance(k, ChatKB)]
        if any(not k.search_ready for k in chat_kbs):
            res = self._search_unindexed(q, targets, chat_ref, senders, ts_from, ts_to,
                                         sort, limit, instant.decode_cursor(sort, cursor)[2])
            res["took_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return res
        query = understand(q or "", list(self.all.values()), identities=ids)
        if query.kbs:
            targets = [k for k in targets if k.id in query.kbs]
        if query.empty:
            return {"hits": [], "total": 0, "query": query.as_dict(),
                    "took_ms": round((time.perf_counter() - t0) * 1000, 1)}
        pairs = []
        for kb in targets:
            if not isinstance(kb, ChatKB):
                continue
            flt = kb.ix_filters(query, chat=chat_ref[1] if chat_ref else None,
                                people=senders.get(kb.id) if senders else None,
                                ts_from=ts_from, ts_to=ts_to)
            if flt is not None:
                pairs.append((kb, flt))
        for kb, _ in pairs:
            kb.ix_catch_up()
        res = instant.run(pairs, query, sort=sort, limit=limit, cursor=cursor,
                          count_cap=count_cap, top=top, correct=correct)
        res["query"] = query.as_dict()
        res["took_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return res

    def facets(self, q: str, *, kbs: str | list[str] | None = None,
               chat: str | None = None, person: str | None = None,
               ts_from: int | None = None, ts_to: int | None = None,
               top: int = 10, correct: bool = True,
               identities: Identities | None = None) -> dict[str, Any]:
        """Counts over what `search` with the same arguments matches: per kb, top chats,
        top people, and every month. The accounts of a same person count as one person.
        Over 20k matches per kb only the newest 20k are counted (`approximate`). When the
        query as written matches nothing, the counts follow the typo-corrected search,
        like `search` does (`correct`)."""
        t0 = time.perf_counter()
        ids = identities or Identities()
        chat_ref = _split(chat)
        senders = _senders(person, ids)
        targets = self._targets(kbs, chat_ref, senders)
        query = understand(q or "", list(self.all.values()), identities=ids)
        if query.kbs:
            targets = [k for k in targets if k.id in query.kbs]
        out: dict[str, Any] = {"total": 0, "approximate": False, "kbs": [], "chats": [],
                               "people": [], "months": [], "query": query.as_dict()}
        if query.empty:
            return out
        plans = []
        for kb in targets:
            if not isinstance(kb, ChatKB) or not kb.search_ready:
                continue
            flt = kb.ix_filters(query, chat=chat_ref[1] if chat_ref else None,
                                people=senders.get(kb.id) if senders else None,
                                ts_from=ts_from, ts_to=ts_to)
            p = instant.plan(query, flt) if flt else None
            if p is None:
                continue
            kb.ix_catch_up()
            plans.append((kb, flt, p))
        per_kb = [(kb, kb.facets(p, top=top)) for kb, _, p in plans]
        if (not any(f["total"] for _, f in per_kb) and correct and not query.literal
                and query.has_text and plans):
            widened = instant.widen_plans(plans, query)
            if widened is not None:
                per_kb = [(kb, kb.facets(p, top=top)) for kb, _, p in widened[0]]
        months: dict[str, int] = {}
        for kb, f in per_kb:
            out["total"] += f["total"]
            out["approximate"] = out["approximate"] or f["approximate"]
            out["kbs"].append({"kb": kb.id, "title": kb.title, "count": f["total"],
                               "approximate": f["approximate"]})
            out["chats"] += f["chats"]
            out["people"] += f["people"]
            for m in f["months"]:
                months[m["month"]] = months.get(m["month"], 0) + m["count"]
        out["chats"] = sorted(out["chats"], key=lambda x: -x["count"])[:top]
        out["people"] = group_counts(out["people"], ids)[:top]
        out["months"] = [{"month": k, "count": v} for k, v in sorted(months.items())]
        out["took_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return out

    def parse(self, q: str, identities: Identities | None = None) -> dict[str, Any]:
        """The query as the search understands it: text, filters, chips, candidates."""
        return understand(q or "", list(self.all.values()), identities=identities).as_dict()

    def _targets(self, kbs, chat_ref: tuple[str, int] | None,
                 senders: dict[str, list[int]] | None) -> list[ChatKB | DocumentsKB]:
        """The knowledge bases picked, narrowed to the chat's and the person's."""
        targets = self.pick(kbs)
        if chat_ref:
            targets = [k for k in targets if k.id == chat_ref[0]]
        if senders is not None:
            targets = [k for k in targets if k.id in senders]
        return targets

    def _search_unindexed(self, q, targets, chat_ref, senders, ts_from, ts_to, sort,
                          limit, offset) -> dict[str, Any]:
        """While a chat kb's instant index is (re)built in the background (a big kb
        opened with no index or an older index version): plain FTS over messages."""
        want = offset + limit
        hits: list[dict[str, Any]] = []
        total = 0
        for kb in targets:
            if not isinstance(kb, ChatKB):
                continue
            r = kb.search_unindexed(q, chat=chat_ref[1] if chat_ref else None,
                          people=senders.get(kb.id) if senders else None,
                          ts_from=ts_from, ts_to=ts_to, sort=sort, limit=want)
            hits.extend(r["hits"])
            total += r["total"]
        if sort in ("relevance", "best"):
            hits.sort(key=lambda h: h.get("score", 0))      # bm25: lower is better
        elif sort == "oldest":
            hits.sort(key=lambda h: h.get("ts") or 0)
        else:
            hits.sort(key=lambda h: -(h.get("ts") or 0))
        return {"hits": hits[offset:offset + limit], "total": total, "indexing": True,
                "next_cursor": f"o{offset + limit}" if offset + limit < total else None}

    def resolve_refs(self, refs: list[str]) -> dict[str, dict[str, Any] | None]:
        """"whatsapp:48213" -> the message, with its chat; "documents:812" -> the
        document segment with its item. Unknown refs map to None."""
        by_kb: dict[str, list[int]] = {}
        for ref in refs:
            m = REF.match(ref.strip())
            if m:
                by_kb.setdefault(m[1], []).append(int(m[2]))
        out: dict[str, dict[str, Any] | None] = {r: None for r in refs}
        for kid, ids in by_kb.items():
            kb = self.get(kid)
            if isinstance(kb, DocumentsKB):
                # a document segment: text, and a sender only for subtitle speakers
                # ("description" for an image caption), so quote checks apply
                for sid, seg in kb.resolve_segments(ids).items():
                    out[f"{kid}:{sid}"] = seg
                continue
            if not isinstance(kb, ChatKB):
                continue
            for mid, msg in kb.messages_by_ids(ids).items():
                out[f"{kid}:{mid}"] = msg
        return out


def _split(ref: str | None) -> tuple[str, int] | None:
    if not ref:
        return None
    m = REF.match(ref.strip())
    return (m[1], int(m[2])) if m else None


def _senders(person: str | None, ids: Identities) -> dict[str, list[int]] | None:
    """kb -> account ids of the person `person` names (every account of a same person);
    None when there is no person filter."""
    ref = _split(person)
    if ref is None:
        return None
    out: dict[str, list[int]] = {}
    for r in sorted(ids.expand(f"{ref[0]}:{ref[1]}")):
        k, pid = _split(r)
        out.setdefault(k, []).append(pid)
    return out


# config/kb.toml: a source block per kind, [embeddings] and [media], and their keys
SOURCE_KEYS = {
    "chat": {"kind": "str", "title": "str", "platform": "str"},
    # documents: kb/documents.py opt(), documents_describe.py, api/documents.py
    "documents": {"kind": "str", "title": "str", "path": "str", "chunk_over_kb": "num", "describe": "bool", "embed": "bool",
                  "describe_part_chars": "int", "describe_max_parts": "int"},
}
TABLES = {
    # kb/semantic.py, embed.py, sessions.py, passages.py
    "embeddings": {"batch": "int", "max_input_chars": "int", "query_prefix": "str",
                   "workers": "int", "dims": "int", "session_gap_minutes": "num",
                   "session_merge_hours": "num", "session_max_messages": "int",
                   "passage_messages": "int", "passage_chars": "int",
                   "passage_overlap": "int", "embed_header": "bool"},
    # kb/media.py Settings.read
    "media": {"types": "list", "rate_per_minute": "num", "batch": "int",
              "max_mb": "num", "poll_minutes": "num",
              "msgstore": "str", "wa_download": "bool", "wa_media_retry": "bool",
              "wa_retry_per_run": "int", "wa_retry_interval_seconds": "num",
              "wa_retry_wait_seconds": "num"},
}


def read_config(file: str, raw: dict[str, Any], problems) -> dict[str, dict[str, Any]]:
    """kb.toml, each block checked: a source of a kind nobody knows is kept whole
    (_build and the doctor say so)."""
    from ..config import Problem, checked
    out: dict[str, dict[str, Any]] = {}
    for kid, block in raw.items():
        if not isinstance(block, dict):
            problems.append(Problem(file, f"unknown key {kid}, ignored"))
            continue
        known = TABLES.get(kid) or SOURCE_KEYS.get(block.get("kind"))
        out[kid] = block if known is None else checked(file, kid, block, known, problems)
    return out


def _problem(msg: str):
    from ..config import Problem
    return Problem("config/kb.toml", msg)


async def probe(cfg, services) -> dict[str, str]:
    """Settings, Features (messages): at least one chat archive has its connector
    answering (its sources are the connected ones); the detail says when search is words
    only (no Topic search model) and when nothing reranks."""
    from ..settings import features
    kbs = getattr(services, "kbs", None)
    if kbs is not None:
        connectors = getattr(services, "connectors", None)
        chats = [k.title for k in kbs.chats()
                 if connectors is not None and (c := connectors.get(k.id)) is not None
                 and c.status is not None]
    else:                                        # celine doctor: no app, the config's word
        chats = [str(b.get("title") or kid) for kid, b in cfg.kb.items()
                 if b.get("kind") == "chat" and features.allows_connector(cfg, kid)]
    if not chats:
        return {"state": "unset", "detail": "No source connected yet."}
    from .retrieve.rerank import reranker_available
    from .semantic import embeddings_available
    notes = [f"Sources: {', '.join(chats)}."]
    if not embeddings_available(cfg):
        notes.append("Word search only: no topic search model.")
    if not reranker_available(cfg):
        notes.append("No reranking model.")
    return {"state": "ready", "detail": " ".join(notes)}
