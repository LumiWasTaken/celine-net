"""The feed: how a connector fills the archive.

A connector (discord-link, wa-link) owns the account and journals everything it
learns as canonical records with a rising `seq`. The app pulls `GET /feed?after=<seq>`
(NDJSON, one record per line, empty heartbeat lines while idle) and applies the
records here through one Sink. `kv.feed_seq` is this archive's position in the
connector's journal; it is written in the same transaction as the last flush of a
batch, so a crash replays at most one batch, which the Sink's conflict rule makes a
no-op. The record shapes are.

Record kinds:

    message   {"chat": {...}, "message": {...}}      a message, new or seen again
    edit      {"chat_key", "message_key", "ts", "text"}
    delete    {"chat_key", "message_keys": [...], "ts"}
    reaction  {"chat_key", "message_key", "sender", "emoji", "ts"}   not stored
    chat      {"chat": {...}}                         a name or member change
    person    {"person": {...}}                       a name learned
    history   {"chat_key", "end"}                     WhatsApp: the phone's word on a
                                                      chat after a history chunk; not stored

An edit or delete whose message has not arrived yet waits in `feed_pending` and is
applied when it does. Records of an ignored chat (`ignored_chats`, "Delete and
ignore") are skipped.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

import httpx

from .db import kv
from .chat import ChatKB
from .importers import Failed, ImportContext, Unreachable

log = logging.getLogger("celine.kb.feed")

KV_SEQ = "feed_seq"
BATCH = 500
BATCH_SECONDS = 2.0
READ_TIMEOUT = 60.0                 # the connector sends a heartbeat every 15 s
KINDS = {"message", "edit", "delete", "reaction", "chat", "person", "history"}
CHAT_KINDS = {"dm", "group", "channel"}
MESSAGE_TYPES = {"text", "media", "system"}
HISTORY_ENDS = {"complete", "more", "no_access"}


class KeyRefused(Failed):
    """The connector refused this app's key: trying again does not help until the key
    or the connection changes."""


class RecordError(ValueError):
    """A record that does not have the canonical shape."""


# ---- records -----------------------------------------------------------------------------

def _str(d: dict, key: str, *, required: bool = True) -> str | None:
    v = d.get(key)
    if v is None or v == "":
        if required:
            raise RecordError(f"{key} is missing")
        return None
    if not isinstance(v, (str, int)):
        raise RecordError(f"{key} is not a string")
    return str(v)


def _ts(d: dict, key: str = "ts") -> int:
    v = d.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise RecordError(f"{key} is not a time in ms")
    return v


def _person(p: Any) -> dict[str, Any] | None:
    if p is None:
        return None
    if not isinstance(p, dict):
        raise RecordError("a person is not an object")
    out = {"key": _str(p, "key")}
    for k in ("phone", "contact_name", "push_name", "display_name", "username"):
        out[k] = _str(p, k, required=False)
    out["is_me"] = bool(p.get("is_me"))
    return out


def _chat(c: Any) -> dict[str, Any]:
    if not isinstance(c, dict):
        raise RecordError("chat is not an object")
    kind = c.get("kind") or "dm"
    if kind not in CHAT_KINDS:
        raise RecordError(f"chat kind {kind!r}")
    return {"key": _str(c, "key"), "kind": kind, "name": _str(c, "name", required=False),
            "parent": _str(c, "parent", required=False), "peer": _person(c.get("peer"))}


def parse(raw: dict[str, Any]) -> dict[str, Any]:
    """One record, checked and normalised. Raises RecordError."""
    if not isinstance(raw, dict):
        raise RecordError("a record is not an object")
    seq = raw.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
        raise RecordError("seq is not a positive integer")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise RecordError(f"unknown kind {kind!r}")
    out: dict[str, Any] = {"seq": seq, "kind": kind}
    if kind == "message":
        m = raw.get("message")
        if not isinstance(m, dict):
            raise RecordError("message is not an object")
        mtype = m.get("type") or "text"
        if mtype not in MESSAGE_TYPES:
            raise RecordError(f"message type {mtype!r}")
        media = m.get("media")
        if media is not None and not isinstance(media, dict):
            raise RecordError("media is not an object")
        text = m.get("text") or ""
        if not isinstance(text, str):
            raise RecordError("text is not a string")
        out["chat"] = _chat(raw.get("chat"))
        out["message"] = {"key": _str(m, "key"), "ts": _ts(m), "from_me": bool(m.get("from_me")),
                          "sender": _person(m.get("sender")), "type": mtype, "text": text,
                          "media": media or None,
                          "reply_key": _str(m, "reply_key", required=False),
                          "edited": bool(m.get("edited")),
                          # the connector sent it (the bridge's send): never a prompt
                          "from_link": bool(m.get("from_link"))}
    elif kind == "edit":
        text = raw.get("text")
        if not isinstance(text, str):
            raise RecordError("text is not a string")
        out.update(chat_key=_str(raw, "chat_key"), message_key=_str(raw, "message_key"),
                   ts=_ts(raw), text=text)
    elif kind == "delete":
        keys = raw.get("message_keys")
        if not isinstance(keys, list) or not keys:
            raise RecordError("message_keys is not a list")
        out.update(chat_key=_str(raw, "chat_key"), message_keys=[str(k) for k in keys],
                   ts=_ts(raw))
    elif kind == "reaction":
        out.update(chat_key=_str(raw, "chat_key"), message_key=_str(raw, "message_key"),
                   ts=_ts(raw))
    elif kind == "chat":
        out["chat"] = _chat(raw.get("chat"))
    elif kind == "person":
        out["person"] = _person(raw.get("person"))
        if out["person"] is None:
            raise RecordError("person is missing")
    elif kind == "history":
        end = raw.get("end")
        if end not in HISTORY_ENDS:
            raise RecordError(f"history end {end!r}")
        out.update(chat_key=_str(raw, "chat_key"), end=end)
    return out


def chat_key(r: dict[str, Any]) -> str | None:
    """The chat a parsed record belongs to; None for a person."""
    if "chat" in r:
        return r["chat"]["key"]
    return r.get("chat_key")


# ---- applying ----------------------------------------------------------------------------

@dataclass
class Applied:
    """What one batch did."""
    seq: int = 0                      # the archive's position after the batch
    records: int = 0
    added: int = 0
    edits: int = 0
    deleted: int = 0
    skipped: int = 0                  # records that were not canonical, or not stored
    pending: int = 0                  # edits and deletes still waiting for their message
    # the applied message records, each with `new`: the archive did not have the
    # message before this batch (a record journaled again is not new)
    messages: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {"seq": self.seq, "records": self.records, "added": self.added,
                "edits": self.edits, "deleted": self.deleted, "skipped": self.skipped,
                "pending": self.pending}


def current_seq(kb: ChatKB) -> int:
    v = kb.file.read_sync(lambda c: kv(c, KV_SEQ))
    return int(json.loads(v)) if v else 0


class Consumer:
    """Applies a connector's records to one chat archive."""

    def __init__(self, kb: ChatKB, *, name: str | None = None,
                 on_batch: Callable[[Applied], None] | None = None):
        self.kb = kb
        self.name = name or kb.title
        self.on_batch = on_batch

    @property
    def seq(self) -> int:
        return current_seq(self.kb)

    # ---- one batch -------------------------------------------------------------------
    def apply(self, records: Iterable[dict[str, Any]], *, cursor: bool = True) -> Applied:
        """Apply records in order. Records at or below the archive's position are
        skipped (a replay); a record that is not canonical, or of an ignored chat, is
        skipped and counted. `cursor=False` is for a live tool's result: its records
        are applied whatever their seq and the archive's position does not move (the
        stream brings them again in order, a no-op then), and for a chat included
        again."""
        start = self.seq if cursor else 0
        out = Applied(seq=start)
        parsed: list[dict[str, Any]] = []
        for raw in records:
            out.records += 1
            try:
                r = parse(raw)
            except RecordError as e:
                out.skipped += 1
                log.info("feed record skipped", extra={"event": "feed.skipped",
                                                       "kb": self.kb.id, "error": str(e)})
                if isinstance(raw, dict) and isinstance(raw.get("seq"), int):
                    out.seq = max(out.seq, raw["seq"])
                continue
            if r["seq"] <= start:
                continue
            parsed.append(r)
            out.seq = max(out.seq, r["seq"])
        if not parsed and (not cursor or out.seq == start):
            return out
        sink = self.kb.sink()
        try:
            w = _Writer(sink)
            kept = [r for r in parsed if chat_key(r) not in sink.ignored]
            out.skipped += len(parsed) - len(kept)
            for r in kept:
                w.apply(r, out)
            sink.flush()
            w.pending(out)
            unused = w.unused_files()
            if cursor:
                sink.conn.execute(
                    "INSERT INTO kv (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (KV_SEQ, json.dumps(out.seq)))
            out.pending = sink.conn.execute("SELECT COUNT(*) FROM feed_pending").fetchone()[0]
        except BaseException:
            sink.conn.close()           # what is not committed yet is not kept
            raise
        sink.close()
        if unused:
            from .media import media_dir
            for sha in unused:
                (media_dir(self.kb) / sha).unlink(missing_ok=True)
        out.added = sink.added
        if cursor and self.on_batch is not None:
            try:
                self.on_batch(out)
            except Exception:
                log.exception("feed listener failed")
        return out

    # ---- the stream --------------------------------------------------------------------
    def run(self, ctx: ImportContext, url: str, key: str | None = None, *,
            transport: httpx.BaseTransport | None = None,
            batch: int = BATCH, batch_seconds: float = BATCH_SECONDS) -> dict[str, Any]:
        """Stream `GET <url>/feed?after=<seq>` and apply it in batches until the
        connector closes the stream or the task is cancelled. No answer, or a stream
        that breaks off, ends the run as Unreachable; the next run continues from
        `feed_seq`."""
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        total = Applied(seq=self.seq)
        buf: list[dict[str, Any]] = []
        last = time.monotonic()

        def flush() -> None:
            nonlocal buf, last
            if buf:
                got = self.apply(buf)
                total.seq = got.seq
                total.records += got.records
                total.added += got.added
                total.edits += got.edits
                total.deleted += got.deleted
                total.skipped += got.skipped
                total.pending = got.pending
                ctx.progress(total.records, None, "records")
            buf = []
            last = time.monotonic()

        client = httpx.Client(transport=transport, headers=headers,
                              timeout=httpx.Timeout(READ_TIMEOUT, connect=10.0))
        try:
            with client.stream("GET", f"{url.rstrip('/')}/feed",
                               params={"after": total.seq}) as resp:
                if resp.status_code in (401, 403):
                    raise KeyRefused(f"The {self.name} connection refused this app's key.")
                if resp.status_code != 200:
                    raise Unreachable(f"The {self.name} connection is not reachable.")
                for line in _lines(resp):
                    ctx.check()
                    if line:
                        try:
                            buf.append(json.loads(line))
                        except ValueError:
                            total.skipped += 1
                    if len(buf) >= batch or (buf and time.monotonic() - last >= batch_seconds):
                        flush()
                    elif not line and buf:
                        flush()          # a heartbeat: the connector is idle, apply now
            flush()
        except httpx.HTTPError:
            flush()
            raise Unreachable(f"The {self.name} connection is not reachable.") from None
        finally:
            client.close()
        return total.as_dict()


def _lines(resp: httpx.Response) -> Iterator[str]:
    for line in resp.iter_lines():
        yield line.strip()


class _Writer:
    """The record kinds, applied through one Sink."""

    def __init__(self, sink):
        self.sink = sink
        self.conn: sqlite3.Connection = sink.conn
        self.written: set[tuple[int, str]] = set()     # messages of this batch
        self.files: set[str] = set()     # files of deleted messages (sha256), maybe unused now

    def person(self, p: dict[str, Any] | None) -> int | None:
        if p is None:
            return None
        return self.sink.person(p["key"], phone=p["phone"], contact_name=p["contact_name"],
                                push_name=p["push_name"], display_name=p["display_name"],
                                username=p["username"], is_me=p["is_me"])

    def chat(self, c: dict[str, Any], *, update: bool = False) -> int:
        peer = c["peer"]
        if peer is not None:
            self.person(peer)
        return self.sink.chat(c["key"], kind=c["kind"],
                              name=None if c["kind"] == "dm" and peer else c["name"],
                              parent=c["parent"],
                              person_key=peer["key"] if peer else None, update=update)

    def apply(self, r: dict[str, Any], out: Applied) -> None:
        kind = r["kind"]
        if kind == "message":
            m = r["message"]
            chat_id = self.chat(r["chat"])
            new = (chat_id, m["key"]) not in self.written and self.conn.execute(
                "SELECT 1 FROM messages WHERE chat_id = ? AND key = ?",
                (chat_id, m["key"])).fetchone() is None
            self.written.add((chat_id, m["key"]))
            sender = None if m["from_me"] else self.person(m["sender"])
            self.sink.message(chat_id, m["key"], ts=m["ts"], text=m["text"],
                              from_me=m["from_me"], sender=sender, kind=m["type"],
                              media=m["media"], reply_key=m["reply_key"], edited=m["edited"])
            out.messages.append({**r, "new": new})
        elif kind == "chat":
            self.chat(r["chat"], update=True)
        elif kind == "person":
            self.person(r["person"])
        elif kind == "edit":
            self.conn.execute(
                "INSERT OR IGNORE INTO feed_pending (chat_key, message_key, kind, ts, text) "
                "VALUES (?, ?, 'edit', ?, ?)",
                (r["chat_key"], r["message_key"], r["ts"], r["text"]))
        elif kind == "delete":
            self.conn.executemany(
                "INSERT OR IGNORE INTO feed_pending (chat_key, message_key, kind, ts) "
                "VALUES (?, ?, 'delete', ?)",
                [(r["chat_key"], k, r["ts"]) for k in r["message_keys"]])
        else:                                  # reaction, history: the archive keeps none
            out.skipped += 1

    def erase(self, r: sqlite3.Row) -> None:
        """Deleted on the source: only a tombstone stays (its time and sender); its text,
        files, earlier texts and file details go."""
        if r["media"]:
            self.files |= {f["sha256"] for f in json.loads(r["media"]).get("files") or []
                           if f.get("sha256")}
        self.conn.execute("UPDATE messages SET deleted_at = ?, text = '', media = NULL, "
                          "edited = 0 WHERE id = ?", (r["ts"], r["mid"]))
        self.conn.execute("DELETE FROM message_edits WHERE message_id = ?", (r["mid"],))
        self.conn.execute("DELETE FROM media_fetch WHERE message_id = ?", (r["mid"],))
        self.conn.execute("DELETE FROM wa_media_meta WHERE key = ?", (r["key"],))

    def unused_files(self) -> set[str]:
        """The deleted messages' files no message has any more; their captions go."""
        if not self.files:
            return set()
        used = {r[0] for r in self.conn.execute(
            "SELECT json_extract(f.value, '$.sha256') FROM messages m, "
            "json_each(m.media, '$.files') f WHERE m.media LIKE '%\"files\"%'")}
        gone = self.files - used
        self.conn.executemany("DELETE FROM media_captions WHERE sha256 = ?",
                              [(f,) for f in gone])
        return gone

    def pending(self, out: Applied) -> None:
        """Edits and deletes whose message is in the archive now, oldest first."""
        rows = self.conn.execute(
            """SELECT p.rowid, p.kind, p.ts, p.text, m.id AS mid, m.key, m.text AS old,
                      m.media, m.deleted_at, m.chat_id
               FROM feed_pending p
               JOIN chats c ON c.key = p.chat_key
               JOIN messages m ON m.chat_id = c.id AND m.key = p.message_key
               ORDER BY p.ts""").fetchall()
        for r in rows:
            if r["kind"] == "delete":
                if r["deleted_at"] is None:
                    self.erase(r)
                    out.deleted += 1
            elif r["deleted_at"] is None and r["text"] != r["old"]:
                self.conn.execute("INSERT INTO message_edits (message_id, ts, old_text) "
                                  "VALUES (?, ?, ?)", (r["mid"], r["ts"], r["old"]))
                self.conn.execute("UPDATE messages SET text = ?, edited = 1 WHERE id = ?",
                                  (r["text"], r["mid"]))
                out.edits += 1
            self.sink.touched.add(r["chat_id"])
            self.conn.execute("DELETE FROM feed_pending WHERE rowid = ?", (r["rowid"],))


__all__ = ["Consumer", "Applied", "RecordError", "parse", "chat_key", "current_seq",
           "KV_SEQ"]
