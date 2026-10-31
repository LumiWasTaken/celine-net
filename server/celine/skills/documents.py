"""{{username}}'s Documents: their drawer of files of any kind (stories, exports, PDFs,
notes, images, audio, archives), in folders, shared with Celine. It lists, searches and
reads everything, and writes text files into it. The agent's notebook is its own and separate.

Each document has a title, the description {{username}} wrote, an AI description, and a
mode. A chunked document is cut into passages it searches and reads one or a range at
a time. A full-file document is found by its title and descriptions and read as one
piece. Passages are cited as [[<kb>:<id>]], checked by the citation verifier like chat
messages (kb/__init__.py resolve_refs). An image it reads comes with its description,
and the image itself goes to a model that sees images (`ToolResult.images`).
"""
from __future__ import annotations

from typing import Annotated, Literal

import anyio

from . import ToolContext, ToolResult, guide, tool, described

READ_DEFAULT = 8000
READ_CAP = 40000
SEEN = {"image/jpeg", "image/png", "image/webp", "image/gif"}   # what vision models read
Sort = Literal["name", "added", "changed", "type", "size"]


def _cap(ctx: ToolContext, n: int) -> int:
    if ctx.max_output_tokens:
        return max(500, min(n, int(ctx.max_output_tokens)))
    return n


def _estimate(text: str) -> int:
    from ..turns import tokens
    return tokens.estimate(text, "tool")


def _seg_line(kid: str, s: dict, width: int | None = None) -> str:
    text = s["text"].replace("\n", " ")
    if width and len(text) > width:
        text = text[:width].rstrip() + "…"
    who = f"{s['speaker']}: " if s.get("speaker") else ""
    if s.get("kind") == "caption":
        who = "(description of the image) "
    t = ""
    if s.get("t_ms") is not None:
        sec = s["t_ms"] // 1000
        t = f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d} "
    return f"[{kid}:{s['id']}] {t}{who}{text}"


def _sid(ref: str | None) -> int | None:
    s = str(ref or "").rsplit(":", 1)[-1].strip()
    return int(s) if s.isdigit() else None


def _size(b: int) -> str:
    if not b:
        return "empty"
    return f"{b / 1048576:.1f} MB" if b >= 1048576 else f"{max(1, b // 1024)} KB"


class UserDocuments:
    name = "user_documents"
    guide = guide("user_documents")
    description = described()

    app = None      # the running app (core/services.py), from skills.build

    def _kb(self):
        kbs = self.app.kbs if self.app is not None else None
        k = kbs.documents() if kbs else None
        if k is None:
            raise RuntimeError("Documents is not set up. Add a block with kind = \"documents\" "
                               "to config/kb.toml.")
        return k

    def __init__(self, cfg):
        self.cfg = cfg

    async def prompt_fragment(self, ctx: ToolContext) -> str | None:
        try:
            k = self._kb()
        except RuntimeError:
            return None
        info = await anyio.to_thread.run_sync(k.info)
        n = info["items"]
        return (f"{self.cfg.user}'s Documents, the drawer you share, hold {n} "
                f"document{'s' if n != 1 else ''} in folders. When a question may be "
                f"answered by one, call `user_documents__list` (a folder's contents) or "
                f"`user_documents__search`, then `user_documents__read`. Cite passages as "
                f"[[{k.id}:<id>]] with the ids the tools show. Never cite an AI description "
                f"as if the document said it. `user_documents__write` puts a text file "
                f"into the drawer.")

    @tool()
    async def list(self, ctx: ToolContext,
                   folder: Annotated[str | None, "A folder path, such as stories/drafts. "
                                                 "Empty: the top of Documents."] = None,
                   sort: Annotated[Sort | None, "name (default), added, changed, type or "
                                                "size."] = None,
                   offset: Annotated[int | None, "Skip this many documents. The `more:` "
                                                 "line gives the value."] = None
                   ) -> ToolResult:
        """List one folder of {{username}}'s Documents: its folders (how many documents
        each holds, and their size), then its documents: id, title, mode (chunked or
        full_file), type, size, {{username}}'s description and the AI description. Use it
        to find your way through the folders and to choose the documents that can answer.
        Returns: the folders, 100 documents, then a `more:` line."""
        try:
            k = self._kb()
        except RuntimeError as e:
            return ToolResult(content=str(e), is_error=True)
        from ..kb.documents import item_line
        f = (folder or "").strip().strip("/")
        off = max(0, int(offset or 0))
        how = sort or "name"
        try:
            here = await anyio.to_thread.run_sync(k.folder, f)
            subs = await anyio.to_thread.run_sync(lambda: k.folders_in(f, sort=how))
            res = await anyio.to_thread.run_sync(lambda: k.list_items(
                folder=f, inside=True, sort=how, limit=100, offset=off))
        except FileNotFoundError:
            return ToolResult(content=f"There is no folder {f}. user_documents__list() "
                                      f"shows the top.", is_error=True)
        except ValueError as e:
            return ToolResult(content=str(e), is_error=True)
        lines = [f"{f or 'the top of Documents'}: {len(subs)} "
                 f"folder{'s' if len(subs) != 1 else ''}, {res['total']} "
                 f"document{'s' if res['total'] != 1 else ''}, sorted by {how}"]
        if subs and off == 0:
            lines.append("folders:")
            lines += [f"- {s['path']}/ ({s['items']} document{'s' if s['items'] != 1 else ''}"
                      f", {_size(s['size'])})" for s in subs]
        if res["items"]:
            lines.append("documents" + (f" {off + 1}-{off + len(res['items'])}"
                                        if res["total"] > len(res["items"]) else "") + ":")
            lines += [item_line(it) for it in res["items"]]
        if off + len(res["items"]) < res["total"]:
            args = (f"folder=\"{f}\", " if f else "") + (f"sort=\"{how}\", " if sort else "")
            lines.append(f"more: user_documents__list({args}offset={off + len(res['items'])})")
        text = "\n".join(lines)
        from ..turns import tokens
        text, _, _ = tokens.cut_to(text, _cap(ctx, 12000))
        return ToolResult(content=text, display={"folder": here["path"],
                                                 "folders": len(subs),
                                                 "items": len(res["items"]),
                                                 "total": res["total"]})

    @tool()
    async def search(self, ctx: ToolContext,
                     query: Annotated[str, "Words or a question."],
                     folder: Annotated[str | None, "Only documents under this folder. "
                                                   "Empty: all of them."] = None,
                     documents: Annotated[list[str] | None,
                                          "Only these documents: ids, paths or titles. "
                                          "Leave it empty for all."] = None,
                     limit: Annotated[int | None, "The most documents, default 8, at most "
                                                "30."] = None) -> ToolResult:
        """Search {{username}}'s documents. Chunked documents: passages ranked by word
        search and semantic topic search. Full-file documents and files without text
        (images by what is in them, audio, archives): matched by name and descriptions.
        Do not use it for chats (chat_archive) or your own notes.
        Returns: the best passages per document, each with a reference."""
        try:
            k = self._kb()
        except RuntimeError as e:
            return ToolResult(content=str(e), is_error=True)
        want = max(1, min(int(limit or 8), 30))
        scope_ids = None
        scope_line = "searched: all documents"
        f = (folder or "").strip().strip("/")
        if f:
            try:
                await anyio.to_thread.run_sync(k.folder_dir, f)
            except (FileNotFoundError, ValueError):
                return ToolResult(content=f"There is no folder {f}.", is_error=True)
            scope_ids = await anyio.to_thread.run_sync(k.ids_under, f)
            scope_line = f"searched: the folder {f}"
            if not scope_ids:
                return ToolResult(content=f"{scope_line}\nThe folder holds no documents.",
                                  display={"items": 0})
        if documents:
            found, missing = [], []
            for ref in documents:
                it = await anyio.to_thread.run_sync(k.find_item, ref)
                (found if it else missing).append(it or ref)
            if not found:
                return ToolResult(content=f"No document called "
                                          f"{', '.join(map(str, documents))}.", is_error=True)
            scope_ids = [it["id"] for it in found if scope_ids is None or it["id"] in scope_ids]
            if not scope_ids:
                return ToolResult(content=f"None of those documents is in the folder {f}.",
                                  is_error=True)
            scope_line = "searched: " + ", ".join(f"\"{it['title']}\" (document {it['id']})"
                                                  for it in found
                                                  if it["id"] in scope_ids) + ", nothing else"
            if missing:
                scope_line += f" (not found: {', '.join(map(str, missing))})"
        lit = await anyio.to_thread.run_sync(
            lambda: k.search_segments(query, item_ids=scope_ids, limit=want, any_word=True))
        sem_failed = None
        try:
            sem = await anyio.to_thread.run_sync(lambda: k.semantic_hits(
                query, scope_ids, k=20))
        except Exception as e:                 # noqa: BLE001 - said in the result
            sem, sem_failed = [], (f"Semantic topic search failed ({e}); these are the "
                                   f"word search hits.")
        # merge: word search groups first (by bm25), then documents found only by the
        # semantic topic search
        groups: dict[int, dict] = {}
        order: list[int] = []
        for g in lit["items"]:
            groups[g["item"]["id"]] = {"item": g["item"], "lines": g["best"],
                                       "hits": g["hits"], "name": g["name_match"]}
            order.append(g["item"]["id"])
        if sem:
            segs = await anyio.to_thread.run_sync(lambda: k.resolve_segments(
                [sid for h in sem for sid in h["segments"][:3]]))
            for h in sem:
                g = groups.get(h["item_id"])
                if g is None:
                    it = await anyio.to_thread.run_sync(k.item, h["item_id"])
                    if it is None:
                        continue
                    g = groups[h["item_id"]] = {"item": it, "lines": [], "hits": 0,
                                                "name": False}
                    order.append(h["item_id"])
                have = {s["id"] for s in g["lines"]}
                for sid in h["segments"][:3]:
                    if sid not in have and sid in segs and len(g["lines"]) < 6:
                        g["lines"].append(segs[sid]["segment"])
        from ..kb.documents import item_line
        if not documents and not f:
            info = await anyio.to_thread.run_sync(k.info)
            scope_line += f" ({info['items']} documents)"
        elif f and not documents:
            scope_line += f" ({len(scope_ids)} documents)"
        if sem_failed:
            scope_line += f"\n{sem_failed}"
        if not order:
            return ToolResult(content=f"{scope_line}\nNo match for {query!r}.",
                              display={"items": 0})
        out = [scope_line]
        for iid in order[:want]:
            g = groups[iid]
            it = g["item"]
            if it.get("mode") == "full_file":
                out.append("\n" + item_line(it).removeprefix("- ")
                           + f"\n  full file: read it with "
                             f"user_documents__read(document=\"{it['id']}\")")
                continue
            why = f"{g['hits']} passage{'s' if g['hits'] != 1 else ''} match" if g["hits"] \
                else "found by semantic topic search" if g["lines"] \
                else "its name or description matches"
            out.append(f"\n{item_line(it, full=False).removeprefix('- ')}; {why}")
            for s in g["lines"][:6]:
                where = f"p. {s['page']}: " if s.get("page") else ""
                out.append("  " + _seg_line(k.id, s, 400).replace("] ", f"] {where}", 1))
        out.append(f"\nRead with user_documents__read(ref=\"{k.id}:<id>\") or "
                   f"user_documents__read(document=<id>).")
        text = "\n".join(out)
        from ..turns import tokens
        text, _, _ = tokens.cut_to(text, _cap(ctx, 8000))
        return ToolResult(content=text, display={"items": len(order),
                                                 "hits": lit["total_hits"]})

    @tool()
    async def read(self, ctx: ToolContext,
                   document: Annotated[str | None, "An id, path or title. Reads from the "
                                                   "start."] = None,
                   ref: Annotated[str | None, "A passage reference, such as documents:812. "
                                              "Reads from there."] = None,
                   to: Annotated[str | None, "With `ref`: the last passage of a "
                                             "range."] = None,
                   direction: Annotated[Literal["after", "before", "around"] | None,
                                        "From `ref`: after (default), before, or "
                                        "around."] = None,
                   max_tokens: Annotated[int | None, "The most text you want back."] = None
                   ) -> ToolResult:
        """Read one document. A full-file document comes whole. A chunked document comes
        in parts: from the start, from a passage, or a range of passages. Each part ends
        with `earlier` and `later` references: ref=later reads on, ref=earlier with
        direction="before" reads what comes before. An image comes with its description, and
        you see the image itself when your model sees images. Returns: the header, then
        the text, each line with its reference."""
        try:
            k = self._kb()
        except RuntimeError as e:
            return ToolResult(content=str(e), is_error=True)
        anchor = end = None
        if ref:
            sid = _sid(ref)
            if sid is None:
                return ToolResult(content=f"{ref!r} is not a passage reference like "
                                          f"{k.id}:812.", is_error=True)
            got = await anyio.to_thread.run_sync(k.resolve_segments, [sid])
            if not got:
                return ToolResult(content=f"{ref} does not exist. The document may have "
                                          f"changed: search again.", is_error=True)
            anchor = got[sid]
            iid = anchor["item"]["id"]
            if to:
                tid = _sid(to)
                got = await anyio.to_thread.run_sync(k.resolve_segments, [tid]) \
                    if tid is not None else {}
                if not got or got[tid]["item"]["id"] != iid:
                    return ToolResult(content=f"{to!r} is not a passage of the same "
                                              f"document as {ref}.", is_error=True)
                end = got[tid]
        elif document:
            it = await anyio.to_thread.run_sync(k.find_item, document)
            if it is None:
                return ToolResult(content=f"No document called {document!r}. "
                                          f"user_documents__list shows them.", is_error=True)
            iid = it["id"]
        else:
            return ToolResult(content="Pass `document` or `ref`.", is_error=True)
        it = await anyio.to_thread.run_sync(k.item, iid)
        whole_doc = it.get("mode") == "full_file"
        default = READ_CAP if whole_doc or end is not None else READ_DEFAULT
        budget = _cap(ctx, max(500, min(int(max_tokens or default), READ_CAP)))
        from ..kb.documents import item_line
        head = item_line(it)
        seen = await self._seen(ctx, k, it)
        if it["state"] == "pending" and not it["n_segments"]:
            return ToolResult(content=f"{head}\n\nThe app is still extracting the text. Try "
                                      f"again in a moment.", images=seen)
        if not it["n_segments"]:
            why = it.get("error") or ("its content is not readable text" if it["kind"] == "file"
                                      else "the app found no text in it")
            return ToolResult(content=f"{head}\n\nNothing to read: {why}.", images=seen)
        # a range reads forwards, and so does a document read from its start
        d = "after" if end is not None or anchor is None else (direction or "after")
        seq0 = anchor["segment"]["seq"] if anchor else 0
        if end is not None and end["segment"]["seq"] < seq0:
            seq0, anchor, end = end["segment"]["seq"], end, anchor
        seq1 = end["segment"]["seq"] if end is not None else None

        def window():
            n = it["n_segments"]
            if d == "before":
                lo = max(0, seq0 - 400)
                rows = k.segments(iid, from_seq=lo, limit=seq0 - lo)["segments"]
                rows.reverse()                       # take from the anchor backwards
            elif d == "around":
                lo = max(0, seq0 - 200)
                rows = k.segments(iid, from_seq=lo, limit=400)["segments"]
                rows.sort(key=lambda s: abs(s["seq"] - seq0))
            else:
                span = (seq1 - seq0 + 1) if seq1 is not None else (n if whole_doc else 400)
                rows = k.segments(iid, from_seq=seq0, limit=max(1, min(span, 2000)))["segments"]
            chosen, used = [], _estimate(head) + 60
            for s in rows:
                t = _estimate(s["text"]) + 8
                if chosen and used + t > budget:
                    break
                chosen.append(s)
                used += t
            chosen.sort(key=lambda s: s["seq"])
            return chosen, n
        chosen, n = await anyio.to_thread.run_sync(window)
        if not chosen:
            return ToolResult(content=f"{head}\n\nnothing {'before' if d == 'before' else 'after'} "
                                      f"{ref}")
        lines = [head, ""]
        page = heading = None
        for s in chosen:
            if s.get("page") is not None and s["page"] != page:
                page = s["page"]
                lines.append(f"[{'chapter' if it['kind'] == 'epub' else 'page'} {page}]")
            if s["kind"] == "heading":
                heading = s["text"]
                lines.append(f"[{k.id}:{s['id']}] ## {s['text']}")
                continue
            if s.get("heading") and s["heading"] != heading and s is chosen[0]:
                heading = s["heading"]
                lines.append(f"(under the heading: {heading})")
            lines.append(_seg_line(k.id, s))
        first, last = chosen[0], chosen[-1]
        earlier = f"{k.id}:{first['id']}" if first["seq"] > 0 else None
        nxt = await anyio.to_thread.run_sync(lambda: k.segments(
            iid, from_seq=last["seq"] + 1, limit=1)["segments"])
        later = f"{k.id}:{nxt[0]['id']}" if nxt else None
        whole = first["seq"] == 0 and not later
        pos = "the whole document" if whole else \
            f"passages {first['seq'] + 1}-{last['seq'] + 1} of {n}"
        lines.insert(1, f"({pos})")
        lines.append("")
        lines.append(f"earlier: {earlier or 'none (start of the document)'}; "
                     f"later: {later or 'none (end of the document)'}")
        return ToolResult(content="\n".join(lines),
                          display={"item": iid, "title": it["title"],
                                   "from": first["id"], "to": last["id"],
                                   "earlier": earlier, "later": later},
                          images=seen)

    async def _seen(self, ctx: ToolContext, k, it: dict) -> list[str] | None:
        """An image document as an image of this call's result, with its description
        kept on it, so a model that cannot see images gets the same description and no
        second one is written (attachments.Images.wire)."""
        if it["kind"] != "image" or it["mime"] not in SEEN or ctx.store is None:
            return None
        from .. import attachments
        try:
            data = (await anyio.to_thread.run_sync(
                lambda: k.resolve(it["path"], must_exist=True))).read_bytes()
        except (OSError, ValueError):
            return None
        text = it["ai_description"]
        row = await attachments.save_generated(
            ctx.config, ctx.store, data, it["name"], it["mime"], ctx.tool_call_id,
            in_message=False,
            described=(text, it["ai"]["model"] or "Photo descriptions") if text else None)
        return [row["id"]]

    @tool(read_only=False, destructive=True)
    async def write(self, ctx: ToolContext,
                    path: Annotated[str, "Where in Documents, such as stories/the-lake.md. "
                                         "Folders are created as needed. `.md` is added to a "
                                         "name without .md or .txt."],
                    content: Annotated[str, "The whole text of the file."]) -> ToolResult:
        """Put a text file into {{username}}'s Documents: a letter, a report, an export,
        notes for them. Creates the file and its folders, or replaces a file you wrote
        before. A file {{username}} put there stays theirs: write to a new path instead.
        Returns: the path and the document's id."""
        try:
            k = self._kb()
        except RuntimeError as e:
            return ToolResult(content=str(e), is_error=True)
        try:
            out = await anyio.to_thread.run_sync(
                lambda: k.write(path, content, author="celine"))
        except ValueError as e:
            return ToolResult(content=str(e), is_error=True)
        it = out["item"]
        return ToolResult(content=f"Wrote {out['path']} ({len(content):,} characters), "
                                  f"document {it['id'] if it else '?'}.",
                          display={"path": out["path"], "item": it["id"] if it else None})
