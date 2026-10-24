"""The REST catch-up: DM and server lists, and every synced chat from its cursor.

- first run of a chat: page backwards from the newest message to the start of the
  channel (or the subscription's backfill limit), saving `oldest` after every page,
  so an interrupted backfill resumes where it stopped.
- later runs: page forwards from `newest`, then finish any backfill left open.

Everything read becomes journal records (the feed carries them to the archive).
"""
from __future__ import annotations

from typing import Any

from . import policy
from .errors import Ctx, DiscordError, Unreachable
from .records import DM, GROUP_DM, TEXT_CHANNELS, THREAD_TYPES, dm_name, message, ms, person
from .rest import CDN, Rest
from .state import State, now_ms


def refresh_me(rest: Rest, ctx: Ctx | None = None) -> dict[str, Any]:
    me = rest.get("/users/@me", ctx=ctx)
    rest.state.set("token_rejected", None)
    rest.state.set("account", {"id": str(me["id"]),
                               "name": me.get("global_name") or me.get("username")})
    rest.state.journal([{"kind": "person", "person": person(me, is_me=True)}])
    return me


def _icon(ch: dict[str, Any]) -> str | None:
    if ch.get("type") == GROUP_DM and ch.get("icon"):
        return f"{CDN}/channel-icons/{ch['id']}/{ch['icon']}.png?size=128"
    recips = ch.get("recipients") or []
    if recips and recips[0].get("avatar"):
        return f"{CDN}/avatars/{recips[0]['id']}/{recips[0]['avatar']}.png?size=128"
    return None


def learn_private(state: State, ch: dict[str, Any]) -> bool:
    """A DM or group DM from a list, the gateway or a tool. One seen for the first time
    is left unticked: a DM is read only once the user ticks it. True when it was new."""
    if ch.get("type") not in (DM, GROUP_DM):
        return False
    key = str(ch["id"])
    new = state.channel(key) is None
    recips = ch.get("recipients") or []
    peer = None
    if ch.get("type") == DM:
        if recips:
            peer = recips[0]
        elif ch.get("recipient_ids"):
            peer = {"id": str(ch["recipient_ids"][0])}
    state.learn_channel(key, kind="group" if ch["type"] == GROUP_DM else "dm",
                        type=ch["type"], name=dm_name(ch) if (ch.get("name") or recips)
                        else None, icon=_icon(ch), peer=peer,
                        last_message_id=ch.get("last_message_id"),
                        enabled=0 if new else None)
    return new


def refresh_lists(rest: Rest, ctx: Ctx) -> int:
    """The account's DMs and servers. Returns how many new DMs it found."""
    new = 0
    for ch in rest.get("/users/@me/channels", ctx=ctx):
        new += int(learn_private(rest.state, ch))
    guilds = sorted(({"id": str(g["id"]), "name": g["name"],
                      "icon": f"{CDN}/icons/{g['id']}/{g['icon']}.png?size=128"
                      if g.get("icon") else None}
                     for g in rest.get("/users/@me/guilds", ctx=ctx)),
                    key=lambda g: g["name"].lower())
    rest.state.set("guilds", guilds)
    return new


def guild_channels(rest: Rest, guild_id: str, ctx: Ctx | None = None) -> list[dict[str, Any]]:
    """A server's text channels (stored), each with `sync` (on the include list) and
    `new` (not on the server when the list was last saved)."""
    raw = rest.get(f"/guilds/{guild_id}/channels", ctx=ctx)
    state = rest.state
    gname = policy.guild_name_of(state, guild_id)
    cats = {str(c["id"]): c["name"] for c in raw if c.get("type") == 4}
    cat_pos = {str(c["id"]): c.get("position", 0) for c in raw if c.get("type") == 4}
    for c in raw:
        if c.get("type") in TEXT_CHANNELS or c.get("type") == 4:
            state.learn_channel(str(c["id"]), kind="channel", type=c.get("type"),
                                name=c.get("name"), guild_id=str(guild_id), guild_name=gname,
                                parent_id=str(c["parent_id"]) if c.get("parent_id") else None,
                                last_message_id=c.get("last_message_id"))
    chans = sorted((c for c in raw if c.get("type") in TEXT_CHANNELS),   # Discord's order
                   key=lambda c: (cat_pos.get(str(c.get("parent_id") or ""), -1),
                                  c.get("position", 0)))
    subs = policy.active(state)
    counts = _counts(state)
    seen = state.get(f"channels_seen:{guild_id}")
    out = []
    for c in chans:
        key = str(c["id"])
        cur = state.one("SELECT synced_at FROM cursors WHERE chat_key = ?", (key,))
        out.append({"id": key, "name": f"#{c['name']}",
                    "category": cats.get(str(c.get("parent_id") or "")),
                    "sync": policy.synced_channel(state, subs, key) is not None,
                    "new": seen is not None and key not in seen,
                    "synced_at": cur["synced_at"] if cur else None,
                    "messages": counts.get(key, 0)})
    return out


def _counts(state: State) -> dict[str, int]:
    return {r[0]: r[1] for r in state.q(
        "SELECT chat_key, COUNT(*) FROM seen WHERE kind = 'message' GROUP BY chat_key")}


def targets(state: State) -> dict[str, Any]:
    """The DMs and servers as stored; nothing here asks Discord."""
    counts = _counts(state)
    dms = []
    for r in state.q("SELECT c.*, cu.synced_at FROM channels c LEFT JOIN cursors cu "
                     "ON cu.chat_key = c.key WHERE c.kind IN ('dm', 'group')"):
        d = state.channel(r["key"])
        name = d.get("name") or (d.get("peer") or {}).get("global_name") or \
            (d.get("peer") or {}).get("username") or d["key"]
        dms.append({"id": d["key"], "name": name, "kind": d["kind"], "icon": d.get("icon"),
                    "sync": bool(d["enabled"]), "synced_at": r["synced_at"],
                    "messages": counts.get(d["key"], 0),
                    "last_message_id": d.get("last_message_id")})
    dms.sort(key=lambda d: (not d["sync"], (d["name"] or "").lower()))
    return {"account": state.get("account"),
            "dms": [{k: v for k, v in d.items() if k != "last_message_id"} for d in dms],
            "guilds": [{**g, "channels": None} for g in state.get("guilds", []) or []]}


def set_targets(state: State, changes: dict[str, bool]) -> tuple[int, list[str]]:
    """Include (true) or leave out (false) chats: a DM or group DM by its tick, a server
    channel or thread by its subscription's include list and the ignore list. A chat
    left out is never journaled again and its cursor is dropped, so including it again
    reads it from the start. Returns (changed, the chats included again)."""
    n, back = 0, []
    for key, on in changes.items():
        key = str(key)
        ch = state.channel(key)
        if ch is None:
            continue
        was = not state.is_ignored(key)
        if ch["kind"] in ("dm", "group"):
            state.x("UPDATE channels SET enabled = ? WHERE key = ?", (int(bool(on)), key))
        else:
            sub = policy.get(state, ch["guild_id"]) if ch.get("guild_id") else None
            if on:
                state.unignore([key])
                if sub is not None and ch.get("type") in TEXT_CHANNELS:
                    policy.put(state, ch["guild_id"], seen=False, settings={
                        "channels": [*sub["settings"]["channels"], key]})
            else:
                state.ignore(key)
                if sub is not None and key in sub["settings"]["channels"]:
                    policy.put(state, ch["guild_id"], seen=False, settings={
                        "channels": [c for c in sub["settings"]["channels"] if c != key]})
        if was != bool(on):
            n += 1
            if on:
                back.append(key)
            else:
                state.forget(key)
    return n, back


# ---- the catch-up ------------------------------------------------------------------------

def sync(rest: Rest, ctx: Ctx, *, guild: str | None = None,
         keys: set[str] | None = None) -> dict[str, Any]:
    """Catch up every ticked DM and every channel of a subscribed server. Records the
    attempt, and since when Discord has been unreachable (the schedule then tries
    every 5 minutes)."""
    state = rest.state
    t = now_ms()
    if guild is None and keys is None:
        state.set("sync_last_attempt", t)
    try:
        out = _sync(rest, ctx, guild=guild, keys=keys)
    except Unreachable:
        if guild is None and keys is None:
            state.set("sync_unreachable_since", state.get("sync_unreachable_since") or t)
        raise
    if guild is None and keys is None:
        state.set("sync_unreachable_since", None)
        state.set("last_sync", now_ms())
    return out


def backfill_guild(rest: Rest, guild_id: str, ctx: Ctx) -> dict[str, Any]:
    try:
        guild_channels(rest, guild_id, ctx)
    except (PermissionError, FileNotFoundError):
        raise DiscordError("Discord does not let this account list that server's "
                           "channels.") from None
    return sync(rest, ctx, guild=guild_id)


def backfill_channels(rest: Rest, guild_id: str, before: list[str], ctx: Ctx) -> dict[str, Any]:
    try:
        listed = guild_channels(rest, guild_id, ctx)
    except (PermissionError, FileNotFoundError):
        raise DiscordError("Discord does not let this account list that server's "
                           "channels.") from None
    old = set(before)
    keys = {c["id"] for c in listed if c["sync"] and c["id"] not in old}
    if not keys:
        return {"journaled": 0, "channels": 0, "note": "no new channels"}
    return sync(rest, ctx, guild=guild_id, keys=keys)


def _wanted(state: State, guild: str | None, keys: set[str] | None) -> list[dict[str, Any]]:
    subs = policy.active(state)
    out = []
    for r in state.q("SELECT key FROM channels ORDER BY kind, name"):
        ch = state.channel(r["key"])
        if ch["kind"] in ("dm", "group"):
            if guild is not None or not ch["enabled"]:
                continue
            out.append({"key": ch["key"], "sub": None})
        elif ch.get("type") in TEXT_CHANNELS or ch.get("type") in THREAD_TYPES:
            sub = policy.synced_channel(state, subs, ch["key"])
            if sub is None or (guild is not None and ch.get("guild_id") != guild):
                continue
            out.append({"key": ch["key"], "sub": sub})
    if keys is not None:
        out = [w for w in out if w["key"] in keys]
    return out


def _sync(rest: Rest, ctx: Ctx, *, guild: str | None,
          keys: set[str] | None) -> dict[str, Any]:
    state = rest.state
    try:
        me = refresh_me(rest, ctx)
    except (PermissionError, FileNotFoundError):
        raise DiscordError("Discord refused to show this account's details.") from None
    new_dms = refresh_lists(rest, ctx) if guild is None else 0
    wanted = _wanted(state, guild, keys)
    if not wanted:
        out = {"journaled": 0, "channels": 0, "note": "nothing is included for sync"}
        return {**out, "new_dms": new_dms} if new_dms else out
    blocked = load_blocked(rest) if any(w["sub"] for w in wanted) else set()
    head = state.head
    skipped: list[str] = []
    fetched = [0]
    for i, w in enumerate(wanted):
        ctx.check()
        try:
            _sync_one(rest, w["key"], str(me["id"]), ctx, fetched, i, len(wanted),
                      _Policy(rest, w["sub"], blocked, w["key"]) if w["sub"] else None)
        except (PermissionError, FileNotFoundError):
            skipped.append(w["key"])
    out: dict[str, Any] = {"journaled": state.head - head, "channels": len(wanted)}
    if new_dms:
        out["new_dms"] = new_dms
    if skipped:
        out["skipped"] = skipped
    return out


class _Policy:
    """A subscription's settings on the REST path, for one channel."""

    def __init__(self, rest: Rest, sub: dict[str, Any], blocked: set[str], key: str):
        self.rest = rest
        self.settings = sub["settings"]
        self.blocked = blocked
        b = self.settings["backfill"]
        created = int(sub.get("started_at") or sub.get("created_at") or 0)
        self.oldest_ts = (None if b == "all" else
                          created - 30 * 86_400_000 if b == "30d" else created)

    def keep(self, m: dict) -> bool:
        if self.oldest_ts is not None and ms(m["timestamp"]) < self.oldest_ts:
            return False
        return policy.keep_message(self.settings, m, self.blocked)

    def files(self, atts: list[dict]) -> list[dict] | None:
        if not atts or not self.settings["include_attachments"]:
            return None
        return policy.download_attachments(self.rest.state, atts)


def _sync_one(rest: Rest, key: str, me_id: str, ctx: Ctx, fetched: list[int],
              index: int, count: int, pol: _Policy | None) -> None:
    state = rest.state
    cur = state.one("SELECT * FROM cursors WHERE chat_key = ?", (key,))
    newest = cur["newest"] if cur else None
    oldest = cur["oldest"] if cur else None
    backfilled = bool(cur["backfilled"]) if cur else False

    def save(**kw) -> None:
        state.x("""INSERT INTO cursors (chat_key, newest, oldest, backfilled, synced_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (chat_key) DO UPDATE SET newest = excluded.newest,
                     oldest = excluded.oldest, backfilled = excluded.backfilled,
                     synced_at = excluded.synced_at""",
                (key, kw.get("newest", newest), kw.get("oldest", oldest),
                 int(kw.get("backfilled", backfilled)), now_ms()))

    def write(batch: list[dict]) -> None:
        recs = []
        for m in batch:
            if pol is not None and not pol.keep(m):
                continue
            learn_author_peer(state, key, m, me_id)
            r = message(state, m, me_id,
                        files=pol.files(m.get("attachments") or []) if pol else None)
            if r is not None:
                recs.append(r)
        state.journal(recs)
        if batch:
            state.learn_channel(key, kind=state.channel(key)["kind"],
                                last_message_id=max((m["id"] for m in batch), key=int))
        fetched[0] += len(batch)
        ctx.progress(fetched[0], None, f"messages, chat {index + 1} of {count}")

    if newest is not None:
        after = newest
        while True:
            batch = rest.get(f"/channels/{key}/messages", {"limit": 100, "after": after}, ctx)
            if not batch:
                break
            batch.sort(key=lambda m: int(m["id"]))
            write(batch)
            after = newest = batch[-1]["id"]
            save(newest=newest)
            if len(batch) < 100:
                break

    if not backfilled:
        before = oldest
        while True:
            params: dict[str, Any] = {"limit": 100}
            if before:
                params["before"] = before
            batch = rest.get(f"/channels/{key}/messages", params, ctx)
            if not batch:
                backfilled = True
                save(backfilled=True)
                break
            batch.sort(key=lambda m: int(m["id"]))
            write(batch)
            if newest is None or int(batch[-1]["id"]) > int(newest):
                newest = batch[-1]["id"]
            before = oldest = batch[0]["id"]
            save(newest=newest, oldest=oldest)
            if pol is not None and pol.oldest_ts is not None and \
                    ms(batch[0]["timestamp"]) < pol.oldest_ts:
                break          # the subscription's backfill limit; "all" later continues
            if len(batch) < 100:
                backfilled = True
                save(backfilled=True)
                break
    else:
        save()


def learn_author_peer(state: State, key: str, m: dict, me_id: str) -> None:
    """A 1:1 DM whose other side is not known yet learns it from a message."""
    ch = state.channel(key)
    if ch is None or ch["kind"] != "dm" or ch.get("peer"):
        return
    a = m.get("author") or {}
    if a.get("id") and str(a["id"]) != str(me_id):
        state.learn_channel(key, kind="dm", peer=a)


# ---- files of a message again ---------------------------------------------------------------

def refresh_files(rest: Rest, chat: str, message_id: str, max_mb: float) -> dict[str, Any]:
    """A message read again with fresh attachment links, its files downloaded, the
    record journaled with them. {"state": "ok" | "gone" | "failed", "error", "files"}."""
    try:
        got = rest.get(f"/channels/{chat}/messages", {"around": message_id, "limit": 1})
    except (FileNotFoundError, PermissionError):
        return {"state": "gone", "error": "no access to the channel any more", "files": []}
    m = next((x for x in got or [] if str(x.get("id")) == str(message_id)), None)
    if m is None:
        return {"state": "gone", "error": "the message is no longer on Discord", "files": []}
    atts = m.get("attachments") or []
    files = policy.download_attachments(rest.state, atts, max_mb) if atts else []
    if not any(f.get("sha256") for f in files):
        return {"state": "failed", "files": files,
                "error": "; ".join(str(f.get("skipped")) for f in files
                                   if f.get("skipped")) or "no files"}
    m.setdefault("channel_id", chat)
    rec = message(rest.state, m, (rest.state.get("account") or {}).get("id"), files=files)
    if rec is not None:
        rest.state.journal([rec])
    return {"state": "ok", "error": None, "files": files}


# ---- blocked users ---------------------------------------------------------------------------

def load_blocked(rest: Rest) -> set[str]:
    """Blocked user ids (relationship type 2), cached in kv."""
    try:
        rels = rest.get("/users/@me/relationships")
        ids = sorted({str(r.get("id") or (r.get("user") or {}).get("id"))
                      for r in rels if r.get("type") == 2})
        rest.state.set("blocked_users", ids)
        return set(ids)
    except (DiscordError, Unreachable, PermissionError, FileNotFoundError):
        return set(rest.state.get("blocked_users", []) or [])
