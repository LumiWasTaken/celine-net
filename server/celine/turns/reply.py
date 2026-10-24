"""The agent's stored reply: what the user sees of the text it wrote, and the one place that
touches it (runner.py calls `stored` and nothing else).

The agent's words are never changed. Only citation tokens are touched: their spelling is made
canonical, a wrong one is marked `|invalid`, a good web one is turned into its link.
The steps, in this order:

1. verify.mark_tool_citations   a tool name written as a citation,
                                `[[chat_archive__look_up: ...]]`, is no reference and
                                becomes `[[chat_archive__look_up|invalid]]`
2. web.normalize                `[web:3]` and `[[web:3-5]]` become `[[web:3]]`
3. verify.normalize_refs        `[[kb:1-5]]` becomes `[[kb:1]]`; `[kb:7]` of a chat
                                knowledge base becomes `[[kb:7]]`
4. the citation check           every `[[kb:id]]` was shown to it by a tool result in
   (verify.check, web.problems) its context (a result now a stub or in the summary
                                counts), a quotation before it is what that message
                                says, a speaker named before it is who wrote it;
                                every `[[web:n]]` was listed or read by a web tool
5. verify.mark_invalid          what step 4 found wrong becomes `[[kb:id|invalid]]`
6. web.link                     `[[web:n]]` with a page behind it becomes `[host](url)`

Steps 3 to 5 need knowledge bases and the `verify_citations` switch of the
chat_archive skill (on by default); without them the text goes from step 2 to step 6.
The check never fails the turn: an error inside it logs and returns the text as step 2
left it. Every check appends a `citations.checked` event (refs counted, the invalid
ones listed).
"""
from __future__ import annotations

import logging

import anyio

from ..skills import web
from . import verify
from ..core.event_types import CITATIONS_CHECKED

log = logging.getLogger(__name__)


async def stored(store, cfg, cid: str, mid: str, text: str | None, messages, hidden, kbs) -> str:
    """The text to store for message `mid` of conversation `cid`: `text` as it wrote
    it, its citation tokens checked as the module docstring says. `messages` is its
    context, `hidden` what the context builder cut from it, `kbs` the knowledge bases
    (None: no check, only the web links)."""
    text = verify.mark_tool_citations(text or "")
    seen = list(messages) + list(hidden)
    web_shown = web.shown(seen)
    text = web.normalize(text)
    if not text or not verify.citations(text):
        return text
    if kbs is None or not cfg.r("verify_citations"):
        return web.link(text, web_shown)
    try:
        chat_kbs = kbs.citable()      # the chat sources and Documents
        text = verify.normalize_refs(text, chat_kbs)
        shown = verify.shown_refs(seen)
        names = verify.shown_senders(seen)
        me = {cfg.user}

        def check(t):
            return verify.check(t, shown, kbs.resolve_refs, me_names=me, chat_kbs=chat_kbs,
                                names=names) + web.problems(t, web_shown)

        problems = await anyio.to_thread.run_sync(check, text)
        n_refs = len({f"{m[1]}:{m[2]}" for m in verify.citations(text)})
        await store.append_event(cid, CITATIONS_CHECKED, {
            "message_id": mid, "refs": n_refs,
            "invalid": [{"ref": p.ref, "reason": p.kind} for p in problems]})
        if problems:
            log.info("citations checked", extra={"event": "turn.citations",
                                                 "invalid": len(problems)})
            text = verify.mark_invalid(text, {p.ref for p in problems})
        return web.link(text, web_shown)
    except Exception:
        log.exception("citation check failed", extra={"event": "turn.citations_failed"})
        return text
