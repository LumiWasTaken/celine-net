"""Passages: the retrieval unit of the semantic index.

A chat is cut into sessions (kb/sessions.py); a session into overlapping windows of at
most `max_messages` (8) messages / `max_chars` (450) characters of message text,
`overlap` (2) messages shared with the window before. Each window gets a deterministic header:

    WhatsApp group "Chess club"
    me, Alex, Bea
    Tuesday 14 November 2023, evening

and one line per message (`Alex: text`, `[photo] caption`, replies with who they answer
and, when the parent is outside the window, its first words). The lines are what gets
embedded (no instruction on the document side; the header too if `embed_header`), and
header + lines are what `passages_fts` indexes, so a BM25 query can match a chat name
or a participant together with words of the conversation ("contextual BM25").

Names are resolved at read time. A vector cannot be: the
names are part of the text the model saw. So passages keep the ids (passage_people,
passage_members) and the build keeps the names it rendered with in `kv`. When a
person or chat is renamed, the next build re-renders the chats where the name
appears, and only passages whose text actually changed are embedded again (the hash
of header + text + members decides). Until that build runs, a passage shows the old
name; the message ids it returns always resolve to the current one.

Incremental. Every non-system message ends up in `session_members`. A build looks
at chats that have messages in no session (a Discord backfill, a
WhatsApp history sync arriving with old timestamps), or rows in `passage_dirty`
(edited or deleted messages, via triggers), re-cuts those chats and replaces only the
sessions whose members changed. Passages of a replaced session whose hash reappears
keep their row and vector. Every chat is one transaction; embedding commits every
batch; a cancelled build resumes where it stopped.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import media_text, names
from .db import in_batches, kv, set_kv
from .embed import SemanticError, Vec, quantize
from .sessions import SessionParams, sessionize, signature

log = logging.getLogger("celine.kb.passages")

RENDER_VER = 3

PLATFORMS = {"whatsapp": "WhatsApp", "discord": "Discord", "text": "Chat"}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")
ME = -1


@dataclass(frozen=True)
class PassageParams:
    max_messages: int = 8
    max_chars: int = 450
    overlap: int = 2
    line_chars: int = 700      # a longer message is cut in the passage (FTS has it whole)
    min_chars: int = 20        # a session with less text than this gets no passage
    quote_chars: int = 60      # a reply to a message outside the window quotes this much
    max_people: int = 6        # names in the header, then "and N more"

    @classmethod
    def from_settings(cls, s: dict[str, Any]) -> "PassageParams":
        return cls(max_messages=int(s.get("passage_messages", 8)),
                   max_chars=int(s.get("passage_chars", 450)),
                   overlap=int(s.get("passage_overlap", 2)))


def has_index(conn: sqlite3.Connection) -> bool:
    """Vectors have been written (the width is known)."""
    return conn.execute("SELECT 1 FROM kv WHERE key = 'passages_vec'").fetchone() is not None


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts for the Library card and Celine's prompt. `ready` once a build finished
    with nothing left to embed; search uses passages from then on."""
    r = conn.execute("""SELECT (SELECT COUNT(*) FROM passages) AS p,
                               (SELECT COUNT(*) FROM passages WHERE embedded = 1) AS e""").fetchone()
    return {"passages": r[0], "embedded": r[1], "ready": kv(conn, "passages_ready") == "1"}


# ---- rendering ---------------------------------------------------------------------------

def time_of_day(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def _day(d: datetime) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} {d.year}"


def when(start_ts: int, end_ts: int) -> str:
    a = datetime.fromtimestamp(start_ts / 1000)
    b = datetime.fromtimestamp(end_ts / 1000)
    out = f"{_day(a)}, {time_of_day(a.hour)}"
    if b.date() != a.date():
        out += f" to {_day(b)}, {time_of_day(b.hour)}"
    return out


def chat_label(platform: str, chat: dict[str, Any]) -> str:
    name = chat.get("name") or "?"
    kind = chat.get("kind")
    if kind == "dm":
        return f"{platform} chat with {name}"
    if kind == "channel":
        where = f' in "{chat["parent"]}"' if chat.get("parent") else ""
        return f'{platform} channel "{name}"{where}'
    return f'{platform} group "{name}"'


def header(platform: str, chat: dict[str, Any], people: Sequence[str],
           start_ts: int, end_ts: int, max_people: int = 6) -> str:
    who = ", ".join(people[:max_people])
    if len(people) > max_people:
        who += f" and {len(people) - max_people} more"
    return f"{chat_label(platform, chat)}\n{who}\n{when(start_ts, end_ts)}"


def body(m: dict[str, Any], described: str | None = None) -> str:
    """What a message says, media as a placeholder: `[photo] caption`, or with an image
    caption (kb/media.py) `[photo: a red car in a car park] caption`."""
    text = (m["text"] or "").strip()
    if m["media"]:
        try:
            kind = json.loads(m["media"]).get("type") or "attachment"
        except (ValueError, AttributeError):
            kind = "attachment"
        if described:
            return f"[{kind}: {described}] {text}".strip()
        return f"[{kind}] {text}".strip()
    return text


def _cut(text: str, n: int) -> str:
    text = " ".join(text.split()) if "\n" in text and len(text) > n else text
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def windows(n_chars: Sequence[int], p: PassageParams) -> list[tuple[int, int]]:
    """[start, end) index pairs over one session's rendered messages. A window holds at
    least one message, at most `max_messages`, and stops before `max_chars`. The next
    starts `overlap` messages before the end (never more than half the window back)."""
    out: list[tuple[int, int]] = []
    n = len(n_chars)
    i = 0
    while i < n:
        j, size = i, 0
        while j < n and j - i < p.max_messages and (j == i or size + n_chars[j] <= p.max_chars):
            size += n_chars[j]
            j += 1
        out.append((i, j))
        if j >= n:
            break
        i = max(i + 1, j - min(p.overlap, (j - i) // 2))
    return out


@dataclass
class Rendered:
    first_id: int
    last_id: int
    start_ts: int
    end_ts: int
    members: list[int]
    people: dict[int, int]
    header: str
    text: str

    @property
    def hash(self) -> str:
        h = hashlib.sha1()
        h.update(self.header.encode())
        h.update(b"\0")
        h.update(self.text.encode())
        h.update(b"\0")
        h.update(",".join(map(str, self.members)).encode())
        return h.hexdigest()[:24]


class Renderer:
    """Everything a chat's passages need to be written: names, the chat, the platform."""

    def __init__(self, platform: str, chat: dict[str, Any], people: dict[int, str],
                 p: PassageParams):
        self.platform = platform
        self.chat = chat
        self.people = people
        self.p = p

    def sender(self, m: dict[str, Any]) -> str:
        if m["from_me"]:
            return "me"
        return self.people.get(m["sender_id"], "someone") if m["sender_id"] else "someone"

    def session(self, msgs: list[dict[str, Any]], by_key: dict[str, dict[str, Any]]
                ) -> list[Rendered]:
        shown = [m for m in msgs if m["body"]]
        if sum(len(m["body"]) for m in shown) < self.p.min_chars:
            return []
        sizes = [min(len(m["body"]), self.p.line_chars) for m in shown]
        out = []
        for a, b in windows(sizes, self.p):
            win = shown[a:b]
            ids = {m["id"] for m in win}
            lines = []
            counts: dict[int, int] = {}
            order: list[str] = []
            for m in win:
                who = self.sender(m)
                pid = ME if m["from_me"] else (m["sender_id"] or 0)
                if pid:
                    counts[pid] = counts.get(pid, 0) + 1
                if who not in order:
                    order.append(who)
                tag = ""
                parent = by_key.get(m["reply_key"]) if m["reply_key"] else None
                if parent is not None:
                    tag = f" (reply to {self.sender(parent)}"
                    if parent["id"] not in ids and parent["body"]:
                        tag += f': "{_cut(parent["body"], self.p.quote_chars)}"'
                    tag += ")"
                lines.append(f"{who}{tag}: {_cut(m['body'], self.p.line_chars)}")
            # most active first, "me" wherever I rank
            freq = {w: 0 for w in order}
            for m in win:
                freq[self.sender(m)] += 1
            ppl = sorted(order, key=lambda w: (-freq[w], order.index(w)))
            out.append(Rendered(
                first_id=win[0]["id"], last_id=win[-1]["id"],
                start_ts=win[0]["ts"], end_ts=win[-1]["ts"],
                members=[m["id"] for m in win], people=counts,
                header=header(self.platform, self.chat, ppl, win[0]["ts"], win[-1]["ts"],
                              self.p.max_people),
                text="\n".join(lines)))
        return out


# ---- building ----------------------------------------------------------------------------

MSG_SQL = """SELECT id, key, ts, from_me, sender_id, text, kind, media, reply_key
             FROM messages WHERE chat_id = ? AND kind <> 'system' ORDER BY ts, id"""


def delete_passages(conn: sqlite3.Connection, ids: Iterable[int]) -> None:
    rows = [(i,) for i in ids]
    if not rows:
        return
    for table in ("passage_members", "passage_people", "passage_vecs", "passage_bits"):
        conn.executemany(f"DELETE FROM {table} WHERE passage_id = ?", rows)
    conn.executemany("DELETE FROM passages WHERE id = ?", rows)


def derive_chat(conn: sqlite3.Connection, chat_id: int, r: Renderer, sp: SessionParams,
                *, force: bool = False) -> dict[str, int]:
    """Re-cut one chat and write what changed. Runs inside the caller's transaction."""
    msgs = []
    rows = conn.execute(MSG_SQL, (chat_id,)).fetchall()
    described = media_text.for_rows(conn, rows, short=True)     # image captions
    for row in rows:
        m = dict(row)
        m["body"] = body(m, described.get(m["id"]))
        m["chars"] = len(m["body"])
        msgs.append(m)
    by_key = {m["key"]: m for m in msgs}
    old = {row["sig"]: row["id"] for row in conn.execute(
        "SELECT id, sig FROM sessions WHERE chat_id = ?", (chat_id,))}
    dirty = {row[0] for row in conn.execute(
        "SELECT message_id FROM passage_dirty WHERE chat_id = ?", (chat_id,))}

    keep: set[int] = set()
    todo: list[tuple[list[dict[str, Any]], str, int | None]] = []   # (msgs, sig, reuse id)
    for s in sessionize(msgs, sp):
        sig = signature([m["id"] for m in s])
        sid = old.get(sig)
        if sid is not None and not force and not any(m["id"] in dirty for m in s):
            keep.add(sid)
        else:
            todo.append((s, sig, sid))
    reused_sids = {sid for _, _, sid in todo if sid is not None}
    gone = [sid for sid in old.values() if sid not in keep and sid not in reused_sids]
    stats = {"sessions": 0, "passages": 0, "reused": 0, "dropped": 0}
    if todo or gone:
        touched = list(reused_sids) + gone
        pool: dict[str, list[int]] = {}
        old_ids: set[int] = set()
        for chunk in in_batches(touched):
            for row in conn.execute(
                    f"SELECT id, hash FROM passages WHERE session_id IN ({','.join('?' * len(chunk))})",
                    chunk):
                pool.setdefault(row["hash"], []).append(row["id"])
                old_ids.add(row["id"])
        kept_pids: set[int] = set()
        for s, sig, sid in todo:
            vals = (chat_id, s[0]["id"], s[-1]["id"], s[0]["ts"], s[-1]["ts"], len(s),
                    sum(m["chars"] for m in s), sig)
            if sid is None:
                sid = conn.execute(
                    "INSERT INTO sessions (chat_id, first_id, last_id, start_ts, end_ts, n_msgs, "
                    "n_chars, sig) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", vals).lastrowid
            else:
                conn.execute("UPDATE sessions SET n_chars = ? WHERE id = ?", (vals[6], sid))
            stats["sessions"] += 1
            conn.executemany("INSERT OR REPLACE INTO session_members (message_id, session_id) "
                             "VALUES (?, ?)", [(m["id"], sid) for m in s])
            for seq, p in enumerate(r.session(s, by_key)):
                h = p.hash
                if pool.get(h):
                    pid = pool[h].pop()
                    conn.execute("UPDATE passages SET session_id = ?, seq = ? WHERE id = ?",
                                 (sid, seq, pid))
                    kept_pids.add(pid)
                    stats["reused"] += 1
                    continue
                pid = conn.execute(
                    "INSERT INTO passages (session_id, chat_id, seq, first_id, last_id, start_ts, "
                    "end_ts, n_msgs, header, text, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (sid, chat_id, seq, p.first_id, p.last_id, p.start_ts, p.end_ts,
                     len(p.members), p.header, p.text, h)).lastrowid
                conn.executemany("INSERT INTO passage_members (message_id, passage_id) "
                                 "VALUES (?, ?)", [(mid, pid) for mid in p.members])
                conn.executemany("INSERT INTO passage_people (passage_id, person_id, n) "
                                 "VALUES (?, ?, ?)", [(pid, k, v) for k, v in p.people.items()])
                stats["passages"] += 1
        drop = old_ids - kept_pids
        stats["dropped"] = len(drop)
        delete_passages(conn, drop)
        if gone:
            rows = [(g,) for g in gone]
            conn.executemany("DELETE FROM session_members WHERE session_id = ?", rows)
            conn.executemany("DELETE FROM sessions WHERE id = ?", rows)
    conn.execute("DELETE FROM passage_dirty WHERE chat_id = ?", (chat_id,))
    return stats


def name_maps(conn: sqlite3.Connection, chats: dict[int, dict[str, Any]]
              ) -> tuple[dict[int, str], dict[str, Any]]:
    people = {row["id"]: names.resolve(row)[0] for row in conn.execute("SELECT * FROM people")}
    snap = {"p": {str(k): v for k, v in people.items()},
            "c": {str(k): [c.get("name"), c.get("kind"), c.get("parent")] for k, c in chats.items()}}
    return people, snap


def renamed_chats(conn: sqlite3.Connection, before: dict[str, Any] | None,
                  now: dict[str, Any]) -> set[int]:
    """Chats whose rendered text a rename changes: renamed chats, and chats where a
    renamed person wrote."""
    if not before:
        return set()
    out = {int(k) for k, v in now["c"].items() if k in before.get("c", {})
           and before["c"][k] != v}
    persons = [int(k) for k, v in now["p"].items() if k in before.get("p", {})
               and before["p"][k] != v]
    for chunk in in_batches(persons):
        out |= {row[0] for row in conn.execute(
            f"SELECT DISTINCT chat_id FROM messages WHERE sender_id IN "
            f"({','.join('?' * len(chunk))})", chunk)}
    return out


def pending_chats(conn: sqlite3.Connection) -> set[int]:
    new = {row[0] for row in conn.execute(
        """SELECT DISTINCT m.chat_id FROM messages m
           WHERE m.kind <> 'system'
             AND NOT EXISTS (SELECT 1 FROM session_members sm WHERE sm.message_id = m.id)""")}
    return new | {row[0] for row in conn.execute("SELECT DISTINCT chat_id FROM passage_dirty")}


def params_sig(sp: SessionParams, pp: PassageParams) -> str:
    return json.dumps({"v": RENDER_VER, "s": asdict(sp), "p": asdict(pp)}, sort_keys=True)


def stale_render(conn: sqlite3.Connection) -> bool:
    """Built with another RENDER_VER: the next build cuts every chat again."""
    raw = kv(conn, "passages_params")
    return raw is not None and json.loads(raw).get("v") != RENDER_VER


def build(path: Path, *, platform: str, chats: dict[int, dict[str, Any]],
          sp: SessionParams, pp: PassageParams, check: Callable[[], None],
          progress: Callable[[int, int | None, str], None]) -> dict[str, int]:
    """Pass one: sessions and passages for every chat that needs them."""
    from .db import connect
    conn = connect(path)
    try:
        people, snap = name_maps(conn, chats)
        before = json.loads(kv(conn, "passages_names") or "null")
        psig = params_sig(sp, pp)
        everything = kv(conn, "passages_params") not in (None, psig)
        force = renamed_chats(conn, before, snap)
        todo = pending_chats(conn) | force
        if everything:
            force = {row[0] for row in conn.execute("SELECT DISTINCT chat_id FROM sessions")}
            todo |= force
        total = {"chats": 0, "sessions": 0, "passages": 0, "reused": 0, "dropped": 0}
        label = PLATFORMS.get(platform, platform.title())
        order = sorted(todo)
        conn.commit()
        for i, chat_id in enumerate(order):
            check()
            progress(i, len(order), "chats cut into passages")
            chat = chats.get(chat_id) or {"name": None, "kind": "group"}
            conn.execute("BEGIN IMMEDIATE")
            try:
                st = derive_chat(conn, chat_id, Renderer(label, chat, people, pp), sp,
                                 force=chat_id in force)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            total["chats"] += 1
            for k in ("sessions", "passages", "reused", "dropped"):
                total[k] += st[k]
        set_kv(conn, "passages_names", json.dumps(snap, ensure_ascii=False))
        set_kv(conn, "passages_params", psig)
        conn.commit()
        progress(len(order), len(order), "chats cut into passages")
        return total
    finally:
        conn.close()


# ---- embedding ---------------------------------------------------------------------------

_meta_cache: dict[str, dict[str, Any]] = {}


def vec_meta(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The index's identity: {dims, type, header, label, probes}; None before the
    first vector. `probes` is the fingerprint of the model that made the vectors
    (kb/embed.py); `label` names that model for display only."""
    raw = kv(conn, "passages_vec")
    if not raw:
        return None
    hit = _meta_cache.get(raw)
    if hit is None:
        hit = json.loads(raw)
        if len(_meta_cache) > 16:
            _meta_cache.clear()
        _meta_cache[raw] = hit
    return hit


def matches(conn: sqlite3.Connection, fp: tuple | None) -> bool:
    """Were this index's vectors made by the model with fingerprint `fp`?"""
    from .embed import matches as fp_matches
    meta = vec_meta(conn)
    return meta is not None and fp_matches(meta.get("probes"), fp)


def ensure_vec(conn: sqlite3.Connection, dims: int, fp: tuple, label: str,
               header: bool) -> bool:
    """Make sure the index takes vectors of this model and width. The first vector
    decides; a different model (fingerprint) or width drops every vector and marks
    every passage for embedding again. Meaning search is off for this knowledge base until that is done. Returns
    True when it reset. Caller commits."""
    from .embed import stored_probes
    if dims % 8:
        raise SemanticError(f"{dims} dimensions: the width must be a multiple of 8")
    meta = vec_meta(conn)
    same = meta is not None and meta.get("dims") == dims and matches(conn, fp)
    if same:
        if meta.get("label") != label:
            set_kv(conn, "passages_vec", json.dumps({**meta, "label": label}))
        return False
    had = meta is not None
    if had:
        conn.execute("DELETE FROM passage_vecs")
        conn.execute("DELETE FROM passage_bits")
        conn.execute("UPDATE passages SET embedded = 0")
        set_kv(conn, "passages_ready", "0")
    set_kv(conn, "passages_vec", json.dumps({"dims": dims, "type": "int8+bit",
                                              "header": header, "label": label,
                                              "probes": stored_probes(fp, dims)}))
    return had


def doc_text(header: str, text: str, with_header: bool = True) -> str:
    """What gets embedded: the text alone by default (`embed_header` false); the header
    is still in passages_fts and in every result."""
    return f"{header}\n{text}" if with_header else text


def _header_switch(conn: sqlite3.Connection, header: bool) -> None:
    """`embed_header` changed since the vectors were made: embed everything again
    (the old vectors keep answering until each is replaced)."""
    meta = vec_meta(conn)
    if meta is not None and meta["header"] != header:
        conn.execute("UPDATE passages SET embedded = 0")
        set_kv(conn, "passages_vec", json.dumps({**meta, "header": header}))
        conn.commit()


def embed(path: Path, *, s: dict[str, Any], fp: tuple, label: str, pool: Any,
          check: Callable[[], None],
          progress: Callable[[int, int | None, str], None]) -> int:
    """Pass two: every passage without a vector, newest first (the period asked about
    most is usable first), with the model whose fingerprint is `fp` (ensure_vec).
    Commits every batch; a restart continues."""
    from .db import connect
    conn = connect(path)
    header = bool(s.get("embed_header", False))
    dims_cfg = int(s.get("dims", 1024))
    dims_model = len(fp[0]) if fp else dims_cfg
    dims = min(dims_cfg, dims_model)
    dims -= dims % 8
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if ensure_vec(conn, dims, fp, label, header):
                log.info("index reset for another model", extra={
                    "event": "kb.semantic.reset", "path": str(path.name), "model": label})
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        _header_switch(conn, header)
        rows = conn.execute("SELECT id, hash, chat_id, start_ts, header, text FROM passages "
                            "WHERE embedded = 0 ORDER BY start_ts DESC, id DESC").fetchall()
        total = len(rows)
        if not total:
            _finish(conn)
            return 0
        size = max(1, int(s.get("batch", 64)))
        batches = [rows[i:i + size] for i in range(0, total, size)]
        done = 0

        def store(i: int, vecs: list[list[float]]) -> None:
            nonlocal done
            batch = batches[i]
            if len(vecs) != len(batch):
                raise SemanticError("the embedding server returned the wrong number of vectors")
            qs = [quantize(v, dims) for v in vecs]
            conn.execute("BEGIN IMMEDIATE")
            try:
                for r, q in zip(batch, qs):
                    cur = conn.execute("UPDATE passages SET embedded = 1 WHERE id = ? AND hash = ?",
                                       (r["id"], r["hash"]))
                    if not cur.rowcount:
                        continue              # re-cut while we were embedding
                    conn.execute("INSERT OR REPLACE INTO passage_vecs (passage_id, v) VALUES (?, ?)",
                                 (r["id"], q.int8))
                    conn.execute("INSERT OR REPLACE INTO passage_bits (passage_id, chat_id, "
                                 "start_ts, b) VALUES (?, ?, ?, ?)",
                                 (r["id"], r["chat_id"], r["start_ts"], q.bits))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            done += len(batch)
            progress(done, total, "passages embedded")

        pool.run([[doc_text(r["header"], r["text"], header) for r in b] for b in batches],
                 store, check)
        _finish(conn)
        return done
    finally:
        conn.close()


def _finish(conn: sqlite3.Connection) -> None:
    left = conn.execute("SELECT COUNT(*) FROM passages WHERE embedded = 0").fetchone()[0]
    if not left and conn.execute("SELECT 1 FROM passages LIMIT 1").fetchone():
        set_kv(conn, "passages_ready", "1")
        conn.commit()


# ---- search ------------------------------------------------------------------------------

EXACT_MAX = 6000          # a filter that leaves at most this many passages is scored exactly


def _filters(chat_ids: Sequence[int] | None, ts_from: int | None, ts_to: int | None,
             col: str = "") -> tuple[str, list[Any]]:
    where, args = [], []
    if chat_ids is not None:
        where.append(f"{col}chat_id IN ({','.join('?' * len(chat_ids)) or 'NULL'})")
        args.extend(int(c) for c in chat_ids)
    if ts_from is not None:
        where.append(f"{col}start_ts >= ?")
        args.append(int(ts_from))
    if ts_to is not None:
        where.append(f"{col}start_ts < ?")
        args.append(int(ts_to))
    return "".join(f" AND {w}" for w in where), args


def dense(conn: sqlite3.Connection, q: Vec, k: int = 50, *,
          chat_ids: Sequence[int] | None = None, ts_from: int | None = None,
          ts_to: int | None = None) -> list[tuple[int, float]]:
    """(passage id, cosine similarity), best first. Empty when another model (its
    fingerprint) made the index.

    Hamming distance over the sign bits picks max(400, 10 k) candidates, int8 cosine
    ranks them. A filter that leaves at most EXACT_MAX
    passages skips the bits and scores all of them exactly.

    Plain tables and sqlite-vec's scalar distance functions, not vec0: measured on
    30k passages (sqlite-vec 0.1.9), a vec0 int8 cosine KNN takes ~60 ms, a vec0 bit
    KNN grows with k (k=400: 18 ms), vec0 `IN` on a partition key returns wrong rows,
    while `ORDER BY vec_distance_hamming(...) LIMIT 400` over a 4 MB table takes 5 ms
    and rescoring 400 int8 rows 1.3 ms."""
    meta = vec_meta(conn)
    if meta is None:
        return []
    if q.fp is not None and not matches(conn, q.fp):
        return []
    if meta["dims"] != q.dims:
        raise SemanticError("the query vector and the index have different widths")
    where, args = _filters(chat_ids, ts_from, ts_to, "b.")
    if where:
        n = conn.execute(f"SELECT COUNT(*) FROM passage_bits b WHERE 1{where}", args).fetchone()[0]
        if n <= EXACT_MAX:
            rows = conn.execute(
                f"SELECT b.passage_id, vec_distance_cosine(vec_int8(v.v), vec_int8(?)) AS d "
                f"FROM passage_bits b JOIN passage_vecs v ON v.passage_id = b.passage_id "
                f"WHERE 1{where} ORDER BY d LIMIT ?", [q.int8, *args, k]).fetchall()
            return [(r[0], round(1 - r[1], 4)) for r in rows]
    n_cand = max(400, 10 * k)
    cand = [r[0] for r in conn.execute(
        f"SELECT b.passage_id FROM passage_bits b WHERE 1{where} "
        f"ORDER BY vec_distance_hamming(vec_bit(b.b), vec_bit(?)) LIMIT ?",
        [*args, q.bits, n_cand])]
    if not cand:
        return []
    rows = conn.execute(
        f"SELECT passage_id, vec_distance_cosine(vec_int8(v), vec_int8(?)) AS d FROM passage_vecs "
        f"WHERE passage_id IN ({','.join(map(str, cand))}) ORDER BY d LIMIT ?",
        (q.int8, k)).fetchall()
    return [(r[0], round(1 - r[1], 4)) for r in rows]


def load(conn: sqlite3.Connection, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Passages by id with their message ids (time order) and person ids (-1 = me)."""
    out: dict[int, dict[str, Any]] = {}
    for chunk in in_batches(ids):
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT * FROM passages WHERE id IN ({marks})", chunk):
            out[r["id"]] = {"passage": r["id"], "session": r["session_id"], "chat_id": r["chat_id"],
                            "seq": r["seq"], "start_ts": r["start_ts"], "end_ts": r["end_ts"],
                            "header": r["header"], "text": r["text"], "messages": [],
                            "people": []}
        for r in conn.execute(
                f"SELECT pm.passage_id, pm.message_id FROM passage_members pm "
                f"JOIN messages m ON m.id = pm.message_id "
                f"WHERE pm.passage_id IN ({marks}) ORDER BY m.ts, m.id", chunk):
            if r[0] in out:
                out[r[0]]["messages"].append(r[1])
        for r in conn.execute(
                f"SELECT passage_id, person_id FROM passage_people WHERE passage_id IN ({marks}) "
                f"ORDER BY n DESC", chunk):
            if r[0] in out:
                out[r[0]]["people"].append(r[1])
    return out
