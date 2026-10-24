"""Delete and ignore: a chat leaves the archive, and no sync or import brings it back
until it is included again. One list for every chat source: `ignored_chats` in the
archive, which every Sink keeps to (kb/chat.py). The connector adds what only it can:

- Discord: discord-link leaves the chat out too, so it stops reading it; including it
  again makes discord-link read it from the start.
- WhatsApp: the phone sends a chat's history once, so wa-link's journal keeps every
  chat; including one again applies what the journal has of it (`GET /admin/records`).

The source routes (api/kb.py) and `celine purge` (ops.py) both go through here.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from .chat import ChatKB
from .connectors import Refused
from .feed import BATCH, Consumer
from .importers import ImportContext


def bridge_chat(connector) -> str | None:
    """The connection's bridged chat (WhatsApp: Message yourself). It carries Celine's
    conversation, so it is never deleted."""
    if connector is None:
        return None
    return (connector.admin_sync("GET", "/admin/info") or {}).get("write_chat")


def delete_and_ignore(kb: ChatKB, chat_ids: list[int], connector=None) -> dict[str, Any]:
    """Delete these chats from the archive and ignore them. Discord's connector leaves
    them out first. Raises Refused (unknown chat, the bridged chat, the connection said
    no) or Unreachable."""
    ids = sorted({int(i) for i in chat_ids})
    if not ids:
        return {"chats": [], "messages": 0}
    keys = dict(kb.file.read_sync(lambda c: [tuple(r) for r in c.execute(
        f"SELECT id, key FROM chats WHERE id IN ({','.join('?' * len(ids))})", ids)]))
    if len(keys) != len(ids):
        raise Refused("There is no such chat.")
    bridge = bridge_chat(connector)
    if bridge is not None and bridge in keys.values():
        raise Refused("This is the bridged chat. You cannot delete it.")
    if connector is not None and kb.platform == "discord":
        connector.admin_sync("PUT", "/admin/targets", {"set": {k: False for k in keys.values()}})
    return kb.delete_chats(ids)


def include_again(kb: ChatKB, key: str, connector=None) -> str | None:
    """Stop ignoring a chat. Discord: returns the connector's job that reads it from
    the start. WhatsApp: run `replay` next (a task). Raises Refused when the chat is
    not ignored."""
    if key not in {c["key"] for c in kb.ignored_chats()}:
        raise Refused("This chat is not ignored.")
    job = None
    if connector is not None and kb.platform == "discord":
        job = (connector.admin_sync("PUT", "/admin/targets", {"set": {key: True}}) or {}).get("job")
    kb.unignore([key])
    return job


def replay(kb: ChatKB, connector, key: str, ctx: ImportContext) -> dict[str, Any]:
    """WhatsApp: apply every record wa-link's journal has of one chat, and copy its
    files. The feed's position does not move and the bridge sees none of it."""
    consumer = Consumer(kb, name=kb.title)
    total = {"records": 0, "added": 0, "skipped": 0}

    def apply(batch: list[dict[str, Any]]) -> None:
        got = consumer.apply(batch, cursor=False)
        total["records"] += got.records
        total["added"] += got.added
        total["skipped"] += got.skipped
        connector.pull_files(got)
        ctx.progress(total["records"], None, "records")

    r = connector.raw_sync("GET", f"/admin/records?chat={quote(key, safe='')}")
    if r.status_code != 200:
        raise connector.refused(r.status_code)
    batch: list[dict[str, Any]] = []
    for line in r.text.splitlines():
        ctx.check()
        if line.strip():
            batch.append(json.loads(line))
        if len(batch) >= BATCH:
            apply(batch)
            batch = []
    if batch:
        apply(batch)
    return total


__all__ = ["bridge_chat", "delete_and_ignore", "include_again", "replay"]
