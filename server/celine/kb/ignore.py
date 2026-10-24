"""Delete and ignore: a chat leaves the archive, and no sync or import brings it back
until it is included again. One list for every chat source: `ignored_chats` in the
archive, which every Sink keeps to (kb/chat.py). The connector adds what only it can:

- Discord: discord-link leaves the chat out too, so it stops reading it; including it
  again makes discord-link read it from the start.
- WhatsApp: wa-link deletes the chat's records and files from its journal and journals
  nothing of it again (`POST /admin/ignored`); including it again lets what comes from
  then on in (`DELETE /admin/ignored`). Its history is gone with the journal's copy.

The source routes (api/connectors.py) and `celine purge` (ops.py) both go through here.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .chat import ChatKB
from .connectors import Refused


def bridge_chat(connector) -> str | None:
    """The connection's bridged chat (WhatsApp: Message yourself). It carries Celine's
    conversation, so it is never deleted."""
    if connector is None:
        return None
    return (connector.admin_sync("GET", "/admin/info") or {}).get("write_chat")


def delete_and_ignore(kb: ChatKB, chat_ids: list[int], connector=None) -> dict[str, Any]:
    """Delete these chats from the archive and ignore them. The connector leaves them
    out first (Discord) or deletes them from its journal (WhatsApp). Raises Refused
    (unknown chat, the bridged chat, the connection said no) or Unreachable."""
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
    if connector is not None and kb.platform == "whatsapp":
        connector.admin_sync("POST", "/admin/ignored", {"chats": sorted(keys.values())})
    return kb.delete_chats(ids)


def include_again(kb: ChatKB, key: str, connector=None) -> str | None:
    """Stop ignoring a chat. Discord: returns the connector's job that reads it from
    the start. WhatsApp: wa-link journals it again from now on. Raises Refused when the
    chat is not ignored."""
    if key not in {c["key"] for c in kb.ignored_chats()}:
        raise Refused("This chat is not ignored.")
    job = None
    if connector is not None and kb.platform == "discord":
        job = (connector.admin_sync("PUT", "/admin/targets", {"set": {key: True}}) or {}).get("job")
    if connector is not None and kb.platform == "whatsapp":
        connector.admin_sync("DELETE", f"/admin/ignored?chat={quote(key, safe='')}")
    kb.unignore([key])
    return job


__all__ = ["bridge_chat", "delete_and_ignore", "include_again"]
