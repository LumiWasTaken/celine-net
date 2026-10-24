"""Discord payloads as canonical feed records (the app's kb/feed.py reads them)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .state import State

TEXT_TYPES = {0, 19}                  # default, reply
DM, GROUP_DM = 1, 3
TEXT_CHANNELS = {0, 5}                # text, announcement
THREAD_TYPES = {10, 11, 12}
EPOCH = 1_420_070_400_000             # Discord's snowflake epoch, ms


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def snowflake_ms(sid: str | int) -> int:
    return (int(sid) >> 22) + EPOCH


def snowflake_at(ms_: int) -> int:
    """The smallest snowflake at that time (for search ranges)."""
    return max(0, int(ms_) - EPOCH) << 22


def person(u: dict[str, Any] | None, *, is_me: bool = False) -> dict[str, Any] | None:
    if not u or not u.get("id"):
        return None
    p = {"key": str(u["id"]), "display_name": u.get("global_name"),
         "username": u.get("username")}
    if is_me:
        p["is_me"] = True
    return {k: v for k, v in p.items() if v is not None}


def dm_name(ch: dict[str, Any]) -> str:
    if ch.get("name"):
        return ch["name"]
    recips = ch.get("recipients") or []
    return ", ".join(r.get("global_name") or r.get("username") or "?" for r in recips) \
        or "Direct message"


def chat_record(state: State, channel_id: str) -> dict[str, Any]:
    """The chat part of a record, from what the connector knows of the channel."""
    ch = state.channel(channel_id) or {"kind": "channel", "name": None}
    if ch["kind"] == "dm":
        peer = person(ch.get("peer"))
        out = {"key": str(channel_id), "kind": "dm"}
        if peer:
            out["peer"] = peer
        else:
            out["name"] = ch.get("name")
        return out
    if ch["kind"] == "group":
        return {"key": str(channel_id), "kind": "group", "name": ch.get("name")}
    name = ch.get("name")
    if ch.get("type") in THREAD_TYPES and ch.get("parent_id"):
        parent = state.channel(ch["parent_id"])
        pname = (parent or {}).get("name")
        name = f"#{pname} › {name}" if pname else f"#{name}"
    elif name:
        name = f"#{name}"
    out = {"key": str(channel_id), "kind": "channel", "name": name or str(channel_id)}
    if ch.get("guild_name"):
        out["parent"] = ch["guild_name"]
    return out


MENTION = re.compile(r"<(@!?|#)(\d+)>")


def plain(text: str, m: dict[str, Any], state: State) -> str:
    """Discord's mention codes as names: `<@id>` from the message's own `mentions` list,
    `<#id>` from the channels the link knows. An unknown one stays as sent."""
    users = {str(u["id"]): u.get("global_name") or u.get("username")
             for u in m.get("mentions") or [] if u.get("id")}

    def one(x: re.Match) -> str:
        if x.group(1) == "#":
            ch = state.channel(x.group(2))
            return f"#{ch['name']}" if ch and ch.get("name") else x.group(0)
        name = users.get(x.group(2))
        return f"@{name}" if name else x.group(0)

    return MENTION.sub(one, text)


def message(state: State, m: dict[str, Any], me_id: str | None,
            files: list[dict] | None = None) -> dict[str, Any] | None:
    """A message record, or None when there is nothing to keep (an empty system event)."""
    a = m.get("author") or {}
    from_me = bool(me_id) and str(a.get("id")) == str(me_id)
    text = plain(m.get("content") or "", m, state)
    atts = m.get("attachments") or []
    stickers = m.get("sticker_items") or []
    kind, media = "text", None
    if m.get("type", 0) not in TEXT_TYPES:
        kind = "system"
        if not text:
            return None
    elif atts or stickers:
        kind = "media"
        if atts:
            ct = atts[0].get("content_type") or ""
            mt = ("photo" if ct.startswith("image/") and "gif" not in ct else
                  "gif" if "gif" in ct else "video" if ct.startswith("video/") else
                  "voice" if (atts[0].get("flags") or 0) & 4 or atts[0].get("waveform") else
                  "audio" if ct.startswith("audio/") else "file")
            media = {"type": mt, "name": atts[0].get("filename")}
        else:
            media = {"type": "sticker", "name": stickers[0].get("name")}
        if text:
            media["caption"] = text
        if files:
            media["files"] = files
    elif not text:
        return None
    msg: dict[str, Any] = {"key": str(m["id"]), "ts": ms(m["timestamp"]), "from_me": from_me,
                           "type": kind, "text": text}
    if not from_me and person(a):
        msg["sender"] = person(a)
    if media:
        msg["media"] = media
    ref = (m.get("message_reference") or {}).get("message_id")
    if ref:
        msg["reply_key"] = str(ref)
    if m.get("edited_timestamp"):
        msg["edited"] = True
    return {"kind": "message", "chat": chat_record(state, str(m["channel_id"])), "message": msg}
