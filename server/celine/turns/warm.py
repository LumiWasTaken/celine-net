"""Keep the chat ready (Settings, Advanced, Models): for that many minutes after the
newest chat's last reply, its request goes to its model server again every WARM_EVERY
seconds, with room for one token. A server that unloads an idle model (Ollama's
keep_alive, llama-swap's ttl, a router's idle timer) keeps it loaded, and the chat's
prompt stays in its cache, so the next message starts without reading the whole chat
again. The request is the next turn's (context.build, dry) through the chat's last user
message: every chat template renders that part the same once the next message follows.
0 minutes: off.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import anyio

from ..backends.base import ORIGIN, BackendError
from ..backends.queue import Priority
from ..core.states import LIVE
from . import context
from .compact import complete
from .prompt import request_parts

log = logging.getLogger("celine.warm")

WARM_EVERY = 240     # seconds, under the 5 minutes many model servers keep an idle model


async def run(services) -> None:
    """The app's loop: one look every WARM_EVERY seconds."""
    while True:
        await anyio.sleep(WARM_EVERY)
        try:
            await once(services)
        except Exception:
            log.warning("keeping the chat ready failed", exc_info=True,
                        extra={"event": "warm.failed"})


async def once(services, at: datetime | None = None) -> str | None:
    """Sends the newest chat's request once while its last reply is recent enough.
    -> the conversation it went for, None when nothing went."""
    cfg, store = services.cfg, services.store
    minutes = float(cfg.r("keep_warm_minutes") or 0)
    if not minutes:
        return None
    row = await store.read(lambda c: c.execute(
        "SELECT t.conversation_id, t.state, t.finished_at FROM turns t "
        "JOIN conversations c ON c.id = t.conversation_id "
        "WHERE c.kind = 'chat' AND c.agent_of IS NULL ORDER BY t.rowid DESC LIMIT 1"
    ).fetchone())
    since = (at or datetime.now(timezone.utc)) - timedelta(minutes=minutes)
    if row is None or row["state"] in LIVE or not row["finished_at"] or \
            datetime.fromisoformat(row["finished_at"]) < since:
        return None
    cid = row["conversation_id"]
    images = None
    if services.llm is not None:
        from ..attachments import Images
        images = Images(cfg, store, services.llm, lane=cid)
    backend = services.dispatcher.bound(cid, role="chat", priority=Priority.BACKGROUND,
                                        body={"max_tokens": 1}, images=images,
                                        fallback=False, wait=WARM_EVERY)
    pieces, tools, _ = await request_parts(store, cfg, services.registry, backend, cid)
    built = await context.build(store, cfg, backend, cid, tools=tools, mode="dry",
                                system="\n\n".join(text for _, text in pieces))
    messages = through_last_user(built.messages)
    if messages is None:
        return None
    try:
        await complete(backend, messages, WARM_EVERY, tools=tools, errors=True)
    except BackendError as e:
        log.info("chat not kept ready", extra={"event": "warm.skipped", "error": str(e)})
        return None
    return cid


def through_last_user(messages: list[dict]) -> list[dict] | None:
    """The messages through the last user message a chat template reads as the user's
    (a result's screen is none); None without one."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m["role"] == "user" and not any(o.get("note") == "screen"
                                            for o in m.get(ORIGIN) or ()):
            return messages[: i + 1]
    return None
