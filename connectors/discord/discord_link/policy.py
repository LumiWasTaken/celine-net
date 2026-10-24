"""What the connector reads: DMs by their tick, servers only when subscribed, and
of a subscribed server only the channels on its include list.

A subscription's settings apply to both paths that journal server messages, the live
gateway and the REST catch-up. Nothing from a server is journaled unless its
subscription is on, except
what Celine reads on purpose with a live tool. Threads follow their channel. A chat the
user ignored (state.ignored) is never journaled, whatever reads it.

The include list: channels the user ticked. A channel that appears on the server later
is not synced until ticked; `channels_seen:<guild>` (kv) holds the channels the server
had when the list was last saved, so the pop-up can show the others as new.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from .records import TEXT_CHANNELS, THREAD_TYPES
from .state import State, now_ms

DEFAULTS: dict[str, Any] = {
    "channels": [],               # the include list: channel ids; threads follow their channel
    "include_bots": False,
    "include_attachments": True,  # downloaded into <state>/media/<sha256>
    "backfill": "30d",            # "none" | "30d" | "all": REST history when starting
}
BACKFILL = ("none", "30d", "all")
ATTACHMENT_MAX_MB = 25            # Discord's upload limit without Nitro


class SettingsError(ValueError):
    pass


def merge_settings(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    """Defaults, then what is stored, then the change. Unknown keys are refused."""
    out = {**DEFAULTS, **(old or {})}
    for k, v in (new or {}).items():
        if k not in DEFAULTS:
            raise SettingsError(f"unknown setting {k!r}")
        if k == "channels":
            if not (isinstance(v, list) and all(isinstance(x, (str, int)) for x in v)):
                raise SettingsError("channels is a list of channel ids")
            v = sorted({str(x) for x in v})
        elif k == "backfill":
            if v not in BACKFILL:
                raise SettingsError(f"backfill is one of {', '.join(BACKFILL)}")
        elif not isinstance(v, bool):
            raise SettingsError(f"{k} is true or false")
        out[k] = v
    return out


def _row(state: State, r) -> dict[str, Any]:
    try:
        stored = json.loads(r["settings"] or "{}")
    except ValueError:
        stored = {}
    st = "on" if r["enabled"] else ("draft" if r["started_at"] is None else "off")
    return {"guild_id": r["guild_id"], "guild_name": r["guild_name"], "icon": r["icon"],
            "enabled": bool(r["enabled"]), "state": st, "created_at": r["created_at"],
            "started_at": r["started_at"], "updated_at": r["updated_at"],
            "settings": {**DEFAULTS, **stored}, "last_event_at": r["last_event_at"],
            "events": r["events"]}


def active(state: State) -> dict[str, dict[str, Any]]:
    """The servers read along."""
    return {r["guild_id"]: _row(state, r)
            for r in state.q("SELECT * FROM subscriptions WHERE enabled = 1")}


def get(state: State, guild_id: str) -> dict[str, Any] | None:
    r = state.one("SELECT * FROM subscriptions WHERE guild_id = ?", (str(guild_id),))
    return {**_row(state, r), "stats": _stats(state, r["guild_id"])} if r else None


def list_all(state: State) -> list[dict[str, Any]]:
    return [{**_row(state, r), "stats": _stats(state, r["guild_id"])} for r in state.q(
        "SELECT * FROM subscriptions ORDER BY enabled DESC, guild_name COLLATE NOCASE")]


def _stats(state: State, guild_id: str) -> dict[str, int]:
    """What the journal holds from the server's channels."""
    chans = [r["key"] for r in state.q("SELECT key FROM channels WHERE guild_id = ?",
                                       (guild_id,))]
    if not chans:
        return {"channels": 0, "messages": 0, "deleted": 0, "edited": 0}
    marks = ",".join("?" * len(chans))
    row = state.one(
        f"""SELECT COUNT(DISTINCT CASE WHEN kind = 'message' THEN chat_key END),
                   SUM(kind = 'message'), SUM(kind = 'delete'), SUM(kind = 'edit')
            FROM seen WHERE chat_key IN ({marks})""", chans)
    return {"channels": row[0] or 0, "messages": row[1] or 0,
            "deleted": row[2] or 0, "edited": row[3] or 0}


def text_channels(state: State, guild_id: str) -> list[str]:
    """The server's text channels the connector knows (a listing or the gateway)."""
    marks = ",".join("?" * len(TEXT_CHANNELS))
    return [r["key"] for r in state.q(
        f"SELECT key FROM channels WHERE guild_id = ? AND type IN ({marks}) ORDER BY key",
        (str(guild_id), *TEXT_CHANNELS))]


def put(state: State, guild_id: str, *, settings: dict[str, Any] | None = None,
        guild_name: str | None = None, icon: str | None = None,
        pause: bool = False, seen: bool = True) -> tuple[dict[str, Any], list[str] | None]:
    """Create or change a subscription without starting it (a new one is a draft).
    A new include list marks the server's channels as seen (`seen`: the user saw them
    all in the pop-up) and takes its channels off the ignore list.
    Returns (subscription, channels_before): the old channel choice when a running
    subscription now takes channels it did not take before, else None."""
    guild_id = str(guild_id)
    old = get(state, guild_id)
    merged = merge_settings(old["settings"] if old else None, settings)
    on = bool(old and old["enabled"]) and not pause
    if guild_name is None:
        guild_name = (old or {}).get("guild_name") or guild_name_of(state, guild_id)
    t = now_ms()
    state.x(
        """INSERT INTO subscriptions (guild_id, guild_name, icon, enabled, created_at,
                                     updated_at, settings)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (guild_id) DO UPDATE SET
             guild_name = COALESCE(excluded.guild_name, subscriptions.guild_name),
             icon = COALESCE(excluded.icon, subscriptions.icon),
             enabled = excluded.enabled, updated_at = excluded.updated_at,
             settings = excluded.settings""",
        (guild_id, guild_name, icon, int(on), t, t,
         json.dumps({k: v for k, v in merged.items() if DEFAULTS.get(k) != v})))
    if settings and "channels" in settings:
        if seen:
            state.set(f"channels_seen:{guild_id}", text_channels(state, guild_id))
        state.unignore(merged["channels"])
    before = old["settings"]["channels"] if old else None
    grew = on and old is not None and bool(set(merged["channels"]) - set(before))
    return get(state, guild_id), (list(before) if grew else None)


def start(state: State, guild_id: str) -> tuple[dict[str, Any], bool]:
    """Start (or resume) reading a server. Returns (subscription, newly_on)."""
    guild_id = str(guild_id)
    old = get(state, guild_id)
    if old is None:
        put(state, guild_id)
        old = get(state, guild_id)
    t = now_ms()
    state.x("UPDATE subscriptions SET enabled = 1, updated_at = ?, "
            "started_at = COALESCE(started_at, ?) WHERE guild_id = ?", (t, t, guild_id))
    return get(state, guild_id), not old["enabled"]


def unsubscribe(state: State, guild_id: str) -> bool:
    return state.x("UPDATE subscriptions SET enabled = 0, updated_at = ? WHERE guild_id = ?",
                   (now_ms(), str(guild_id))) > 0


def note_event(state: State, guild_id: str) -> None:
    state.x("UPDATE subscriptions SET events = events + 1, last_event_at = ? "
            "WHERE guild_id = ?", (now_ms(), str(guild_id)))


def guild_name_of(state: State, guild_id: str) -> str | None:
    """From the server list (each sync refreshes it), else the subscription's name."""
    name = next((g["name"] for g in state.get("guilds", []) or []
                 if g["id"] == str(guild_id)), None)
    if name is None:
        r = state.one("SELECT guild_name FROM subscriptions WHERE guild_id = ?",
                      (str(guild_id),))
        name = r[0] if r else None
    return name


# ---- filtering ------------------------------------------------------------------------------

def channel_allowed(settings: dict[str, Any], channel_id: str, channel_type: int | None,
                    parent_id: str | None) -> bool:
    wanted = set(settings["channels"])
    return channel_id in wanted or (channel_type in THREAD_TYPES and parent_id in wanted)


def keep_message(settings: dict[str, Any] | None, msg: dict[str, Any],
                 blocked: set[str]) -> bool:
    """Server messages only (settings is None for DMs, which are always kept)."""
    if settings is None:
        return True
    author = msg.get("author") or {}
    if author.get("bot") and not settings["include_bots"]:
        return False
    return str(author.get("id")) not in blocked


def synced_channel(state: State, subs: dict[str, dict], key: str) -> dict[str, Any] | None:
    """The subscription settings when a server channel is read along, else None."""
    ch = state.channel(key)
    if ch is None or not ch.get("guild_id") or state.is_ignored(key):
        return None
    sub = subs.get(ch["guild_id"])
    if sub is None:
        return None
    ok = channel_allowed(sub["settings"], key, ch.get("type"), ch.get("parent_id"))
    return sub if ok else None


# ---- attachments ------------------------------------------------------------------------------

def download_attachments(state: State, atts: list[dict[str, Any]],
                         max_mb: float = ATTACHMENT_MAX_MB,
                         client: httpx.Client | None = None) -> list[dict[str, Any]]:
    """Fetch each attachment once into <state>/media/<sha256> (Discord's CDN links
    expire, so this happens when the message is seen). Returns the file records."""
    out: list[dict[str, Any]] = []
    limit = int(max_mb * 1024 * 1024)
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60.0), follow_redirects=True)
    try:
        for a in atts:
            rec = {"name": a.get("filename"), "size": a.get("size"),
                   "content_type": a.get("content_type")}
            if (a.get("size") or 0) > limit:
                rec["skipped"] = f"larger than {max_mb:g} MB"
                out.append(rec)
                continue
            url = a.get("url") or a.get("proxy_url")
            if not url:
                continue
            try:
                r = client.get(url)
                if r.status_code != 200 or len(r.content) > limit:
                    rec["skipped"] = f"download failed ({r.status_code})"
                else:
                    sha = hashlib.sha256(r.content).hexdigest()
                    p = state.media / sha
                    if not p.exists():
                        tmp = p.with_suffix(".tmp")
                        tmp.write_bytes(r.content)
                        tmp.replace(p)
                    rec["sha256"] = sha
            except httpx.HTTPError as e:
                rec["skipped"] = f"download failed ({type(e).__name__})"
            out.append(rec)
    finally:
        if own:
            client.close()
    return out
