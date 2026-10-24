"""kind = "chat": a message archive in one SQLite file."""
from __future__ import annotations

import html
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.ids import now_ms
from . import instant, media_text, names
from . import passages as P
from .db import CHAT_MIGRATIONS, KBFile, connect, in_batches
from .normalize import highlight
from .query import Query


_MARK_A, _MARK_B = "\x02", "\x03"


# ---- FTS query building ---------------------------------------------------------

_SYNTAX = re.compile(r'["*()]|\b(AND|OR|NOT|NEAR)\b')
_WORD = re.compile(r"\w+", re.UNICODE)


_OPERATORS = {"AND", "OR", "NOT", "NEAR"}


def _plain_query(q: str, *, drop_operators: bool = False) -> str | None:
    words = _WORD.findall(q)
    if drop_operators:
        words = [w for w in words if w not in _OPERATORS]
    if not words:
        return None
    parts = [f'"{w}"' for w in words[:-1]]
    parts.append(f'"{words[-1]}"*')
    return " ".join(parts)


def fts_query(q: str) -> str | None:
    """Search-as-you-type: every word must appear, the last one as a prefix.
    Anyone who writes FTS5 syntax (quotes, OR, prefix*) gets it passed through; a quote
    still open while typing (`"foo`) is closed for them."""
    q = q.strip()
    if not q:
        return None
    if _SYNTAX.search(q):
        if q.count('"') % 2:
            q += '"'
        return q
    return _plain_query(q)


def fts_candidates(q: str) -> list[str]:
    """What to try, in order: the query as written, then its words alone. Half-typed
    syntax (`foo OR`, a lone `*`) falls through to the words, or to nothing."""
    out = []
    for m in (fts_query(q), _plain_query(q or "", drop_operators=True)):
        if m and m not in out:
            out.append(m)
    return out


def is_query_error(e: sqlite3.OperationalError) -> bool:
    """An FTS5 MATCH that cannot be parsed, as opposed to a database problem."""
    msg = str(e).lower()
    return not any(x in msg for x in ("locked", "busy", "unable to open", "disk i/o",
                                      "no such table", "readonly", "corrupt"))


def snippet_html(raw: str) -> str:
    """FTS5 snippet with sentinel marks -> escaped HTML with <mark> only."""
    esc = html.escape(raw, quote=False)
    return esc.replace(_MARK_A, "<mark>").replace(_MARK_B, "</mark>")


# ---- the importer side ------------------------------------------------------------

class Sink:
    """What a writer (the connector feed) writes to. One connection for the whole run, committed
    every `batch` messages, so a cancelled or failed import keeps what it wrote
    and other writers wait at most one batch. An ignored chat
    (`ignored_chats`) is never written: `chat()` gives None for it and `message()`
    drops what is given for None."""

    def __init__(self, kb: "ChatKB", batch: int = 2000):
        self.kb = kb
        self.batch = batch
        self.conn = connect(kb.file.path)
        # counted by this sink only, so two imports into one knowledge base at the same
        # time each report their own rows
        self.added = 0
        self._people: dict[str, int] = {}
        self._chats: dict[str, int] = {}
        self._pending: list[tuple] = []
        self.touched: set[int] = set()
        self.ignored = {r[0] for r in self.conn.execute("SELECT key FROM ignored_chats")}

    # people ---------------------------------------------------------------------
    def person(self, key: str, *, phone: str | None = None,
               contact_name: str | None = None, push_name: str | None = None,
               display_name: str | None = None, username: str | None = None,
               is_me: bool = False) -> int:
        """Upsert by key. A name the source does not have (None) never erases one
        that an earlier import found."""
        row = self.conn.execute(
            """INSERT INTO people (key, phone, contact_name, push_name, display_name,
                                   username, is_me)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (key) DO UPDATE SET
                 phone        = COALESCE(excluded.phone, people.phone),
                 contact_name = COALESCE(excluded.contact_name, people.contact_name),
                 push_name    = COALESCE(excluded.push_name, people.push_name),
                 display_name = COALESCE(excluded.display_name, people.display_name),
                 username     = COALESCE(excluded.username, people.username),
                 is_me        = MAX(people.is_me, excluded.is_me)
               RETURNING id""",
            (key, phone, _clean(contact_name), _clean(push_name), _clean(display_name),
             _clean(username), int(is_me)),
        ).fetchone()
        self._people[key] = row[0]
        return row[0]

    def person_id(self, key: str) -> int:
        """Id for a key, creating a bare row when the key is new."""
        pid = self._people.get(key)
        if pid is not None:
            return pid
        row = self.conn.execute("SELECT id FROM people WHERE key = ?", (key,)).fetchone()
        if row:
            self._people[key] = row[0]
            return row[0]
        return self.person(key)

    # chats ------------------------------------------------------------------------
    def chat(self, key: str, *, kind: str = "dm", name: str | None = None,
             parent: str | None = None, person_key: str | None = None,
             update: bool = False) -> int | None:
        """`update`: write the row again even when this sink wrote it (a name change)."""
        if key in self.ignored:
            return None
        cached = self._chats.get(key)
        if cached is not None and not update:
            return cached
        pid = self.person_id(person_key) if person_key else None
        row = self.conn.execute(
            """INSERT INTO chats (key, kind, name, parent, person_id)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (key) DO UPDATE SET
                 kind      = excluded.kind,
                 name      = COALESCE(excluded.name, chats.name),
                 parent    = COALESCE(excluded.parent, chats.parent),
                 person_id = COALESCE(excluded.person_id, chats.person_id)
               RETURNING id""",
            (key, kind, _clean(name), _clean(parent), pid),
        ).fetchone()
        self._chats[key] = row[0]
        return row[0]

    # messages -----------------------------------------------------------------------
    def message(self, chat_id: int | None, key: str, *, ts: int, text: str = "",
                from_me: bool = False, sender: int | None = None, kind: str = "text",
                media: dict | None = None, reply_key: str | None = None,
                edited: bool = False) -> None:
        if chat_id is None:
            return
        self._pending.append((chat_id, str(key), sender, int(from_me), int(ts),
                              text or "", kind,
                              json.dumps(media, ensure_ascii=False) if media else None,
                              reply_key, int(edited)))
        self.touched.add(chat_id)
        if len(self._pending) >= self.batch:
            self.flush()

    def flush(self) -> None:
        if self._pending:
            # hold the write lock from before the max(id) read to the commit, so every
            # row above it was inserted by this batch
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            top = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
            self.conn.executemany(
                """INSERT INTO messages (chat_id, key, sender_id, from_me, ts, text, kind,
                                         media, reply_key, edited)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (chat_id, key) DO UPDATE SET
                     sender_id = COALESCE(excluded.sender_id, messages.sender_id),
                     text      = CASE WHEN excluded.edited > messages.edited
                                      THEN excluded.text ELSE messages.text END,
                     edited    = MAX(messages.edited, excluded.edited),
                     reply_key = COALESCE(messages.reply_key, excluded.reply_key),
                     media     = COALESCE(messages.media, excluded.media)
                   WHERE messages.deleted_at IS NULL AND (
                         messages.sender_id IS NOT COALESCE(excluded.sender_id, messages.sender_id)
                      OR excluded.edited > messages.edited
                      OR (messages.reply_key IS NULL AND excluded.reply_key IS NOT NULL)
                      OR (messages.media IS NULL AND excluded.media IS NOT NULL))""",
                self._pending)
            self.added += self.conn.execute("SELECT COUNT(*) FROM messages WHERE id > ?",
                                            (top,)).fetchone()[0]
            self._pending.clear()
        self.conn.commit()

    def close(self) -> None:
        """Flush, refresh the per-chat numbers, commit and close."""
        try:
            self.flush()
            refresh_chats(self.conn, self.touched)
        finally:
            self.conn.close()


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = str(s).strip()
    return s or None


def refresh_chats(conn: sqlite3.Connection, chat_ids: set[int] | None = None) -> None:
    where = ""
    args: list[Any] = []
    if chat_ids is not None:
        if not chat_ids:
            return
        where = f"WHERE id IN ({','.join('?' * len(chat_ids))})"
        args = list(chat_ids)
    conn.execute(
        f"""UPDATE chats SET
              message_count = (SELECT COUNT(*) FROM messages m WHERE m.chat_id = chats.id),
              first_ts = (SELECT MIN(ts) FROM messages m WHERE m.chat_id = chats.id),
              last_ts  = (SELECT MAX(ts) FROM messages m WHERE m.chat_id = chats.id),
              last_message_id = (SELECT id FROM messages m WHERE m.chat_id = chats.id
                                 ORDER BY ts DESC, id DESC LIMIT 1)
            {where}""", args)
    conn.commit()


# ---- the reading side --------------------------------------------------------------

# People with fewer messages are left out of the people lists (the People page, GET
# .../people, a source's count) unless they are in a person record (a same person, or
# an account with an about text or aliases); their messages keep their name.
LISTED_MIN_MESSAGES = 5


def listed_sql(kept: frozenset[int] | set[int] = frozenset()) -> str:
    """The people lists' rule on `p` joined to a per-sender count `s.n`. `kept`: the
    people in a person record, listed whatever their count."""
    extra = f" OR p.id IN ({','.join(str(int(i)) for i in sorted(kept))})" if kept else ""
    return f"(COALESCE(s.n, 0) >= {LISTED_MIN_MESSAGES}{extra})"


_CHAT_NAME = (f"COALESCE(NULLIF(TRIM(c.custom_name), ''), "
              f"CASE WHEN c.kind = 'dm' AND p.id IS NOT NULL THEN {names.SQL_NAME} END, "
              f"c.name, p.phone, c.key)")


@dataclass
class ChatKB:
    id: str
    title: str
    platform: str
    path: Path
    options: dict[str, Any] = field(default_factory=dict)
    kind: str = "chat"

    def __post_init__(self) -> None:
        self.file = KBFile(self.path, CHAT_MIGRATIONS)
        # the instant search index (kb/instant.sql); built inline for a small kb, in
        # the background for a big one, shared by every ChatKB on the same file
        self.indexer = instant.indexer_for(self.path, connect)

    def sink(self) -> Sink:
        return Sink(self)

    # ---- shapes ----------------------------------------------------------------
    _CHAT_SELECT = f"""
        SELECT c.*, {_CHAT_NAME} AS display, p.id AS p_id, p.key AS key_p,
               p.custom_name AS p_custom,
               p.contact_name, p.display_name, p.push_name, p.username, p.phone,
               lm.id AS lm_id, lm.from_me AS lm_from_me, lm.text AS lm_text,
               lm.media AS lm_media,
               {names.SQL_GIVEN.replace('p.', 'lp.')} AS lm_sender,
               lp.phone AS lp_phone, lp.key AS lp_key
        FROM chats c
        LEFT JOIN people p   ON p.id = c.person_id
        LEFT JOIN messages lm ON lm.id = c.last_message_id
        LEFT JOIN people lp  ON lp.id = lm.sender_id
    """

    def list_chats(self, q: str = "", sort: str = "recent", trashed: bool | None = False,
                   limit: int = 200, offset: int = 0) -> dict[str, Any]:
        """The chats with messages; with `trashed` the trashed ones instead, with None
        both (what the indexes name)."""
        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            where = ["c.message_count > 0"]
            args: list[Any] = []
            if trashed is not None:
                where.append(f"c.trashed = {int(trashed)}")
            if q.strip():
                where.append(f"({_CHAT_NAME} LIKE ? OR c.parent LIKE ?)")
                like = f"%{q.strip()}%"
                args += [like, like]
            order = {"name": "display COLLATE NOCASE",
                     "count": "c.message_count DESC"}.get(sort, "c.last_ts DESC")
            w = " AND ".join(where)
            total = conn.execute(
                f"SELECT COUNT(*) FROM chats c LEFT JOIN people p ON p.id = c.person_id "
                f"WHERE {w}", args).fetchone()[0]
            rows = conn.execute(
                f"{self._CHAT_SELECT} WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
                [*args, limit, offset]).fetchall()
            return {"chats": [self._chat_row(r) for r in rows], "total": total}
        return self.file.read_sync(run)

    def _chat_row(self, r: sqlite3.Row) -> dict[str, Any]:
        d = {
            "id": r["id"], "kb": self.id, "name": _chat_name(r), "kind": r["kind"],
            "parent": r["parent"], "trashed": bool(r["trashed"]),
            "messages": r["message_count"], "first_ts": r["first_ts"],
            "last_ts": r["last_ts"], "custom_name": r["custom_name"],
            "person": r["p_id"], "last_preview": None,
        }
        if r["lm_id"] is not None:
            sender = None
            if not r["lm_from_me"] and r["kind"] != "dm" and r["lp_key"] is not None:
                sender = (r["lm_sender"] or names.format_phone(r["lp_phone"])
                          or names.unknown_label(r["lp_key"]))
            d["last_preview"] = {
                "sender": "me" if r["lm_from_me"] else sender,
                "from_me": bool(r["lm_from_me"]),
                "text": (r["lm_text"] or _media_label(r["lm_media"]) or "")[:140],
            }
        return d

    def get_chat(self, chat_id: int) -> dict[str, Any] | None:
        def run(conn: sqlite3.Connection) -> dict[str, Any] | None:
            r = conn.execute(f"{self._CHAT_SELECT} WHERE c.id = ?", (chat_id,)).fetchone()
            if r is None:
                return None
            d = self._chat_row(r)
            members = conn.execute(
                """SELECT p.*, COUNT(*) AS n FROM messages m JOIN people p ON p.id = m.sender_id
                   WHERE m.chat_id = ? GROUP BY p.id ORDER BY n DESC LIMIT 500""",
                (chat_id,)).fetchall()
            d["members"] = [{**names.person(m), "messages": m["n"]} for m in members]
            return d
        return self.file.read_sync(run)

    def update_chat(self, chat_id: int, *, name: Any = ..., trashed: Any = ...) -> dict | None:
        def run(conn: sqlite3.Connection) -> bool:
            if name is not ...:
                conn.execute("UPDATE chats SET custom_name = ? WHERE id = ?",
                             (_clean(name), chat_id))
            if trashed is not ...:
                conn.execute("UPDATE chats SET trashed = ? WHERE id = ?",
                             (int(bool(trashed)), chat_id))
            return conn.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone() \
                is not None
        if not self.file.write_sync(run):
            return None
        return self.get_chat(chat_id)

    def delete_chats(self, chat_ids: list[int]) -> dict[str, Any]:
        """Delete chats and ignore them from now on: no feed record and no import writes
        them again (`ignored_chats`, see Sink) until `unignore`. Everything derived from
        them goes: messages, their edits, fetch notes and WhatsApp file details, the
        meaning-search sessions and passages with their vectors, instant search rows, the
        connector feed's pending changes, and every
        file (copy and caption) no other message has. People stay. Returns
        {"chats": [{"id", "key", "kind", "name"}], "messages"}; unknown ids are left out."""
        from .media import media_dir

        def run(conn: sqlite3.Connection) -> tuple[dict[str, Any], set[str]]:
            conn.execute("BEGIN IMMEDIATE")
            wanted = sorted({int(i) for i in chat_ids})
            chats = []
            for part in in_batches(wanted):
                chats += [self._chat_row(r) | {"key": r["key"]} for r in conn.execute(
                    f"{self._CHAT_SELECT} WHERE c.id IN ({','.join('?' * len(part))})", part)]
            if not chats:
                return {"chats": [], "messages": 0}, set()
            # the writer's connection is new and closed after this, and so are these
            conn.execute("CREATE TEMP TABLE gone_chats (id INTEGER PRIMARY KEY, key TEXT)")
            conn.execute("CREATE TEMP TABLE gone_messages (id INTEGER PRIMARY KEY, key TEXT)")
            conn.executemany("INSERT INTO temp.gone_chats (id, key) VALUES (?, ?)",
                             [(c["id"], c["key"]) for c in chats])
            conn.execute("INSERT INTO temp.gone_messages (id, key) SELECT id, key FROM messages "
                         "WHERE chat_id IN (SELECT id FROM temp.gone_chats)")
            files = {f["sha256"] for (raw,) in conn.execute(
                "SELECT media FROM messages WHERE media IS NOT NULL AND chat_id IN "
                "(SELECT id FROM temp.gone_chats)")
                for f in (json.loads(raw).get("files") or []) if f.get("sha256")}
            gone = "(SELECT id FROM temp.gone_messages)"
            conn.execute(f"DELETE FROM message_edits WHERE message_id IN {gone}")
            conn.execute(f"DELETE FROM media_fetch WHERE message_id IN {gone}")
            conn.execute("DELETE FROM wa_media_meta WHERE key IN "
                         "(SELECT key FROM temp.gone_messages)")
            P.delete_passages(conn, [r[0] for r in conn.execute(
                "SELECT id FROM passages WHERE chat_id IN (SELECT id FROM temp.gone_chats)")])
            conn.execute("DELETE FROM session_members WHERE session_id IN (SELECT id FROM "
                         "sessions WHERE chat_id IN (SELECT id FROM temp.gone_chats))")
            conn.execute("DELETE FROM sessions WHERE chat_id IN "
                         "(SELECT id FROM temp.gone_chats)")
            n = conn.execute("DELETE FROM messages WHERE chat_id IN "
                             "(SELECT id FROM temp.gone_chats)").rowcount
            instant.forget(conn, [r[0] for r in conn.execute(
                "SELECT id FROM temp.gone_messages")])
            conn.execute("DELETE FROM passage_dirty WHERE chat_id IN "
                         "(SELECT id FROM temp.gone_chats)")
            conn.execute("DELETE FROM feed_pending WHERE chat_key IN "
                         "(SELECT key FROM temp.gone_chats)")
            conn.execute("DELETE FROM chats WHERE id IN (SELECT id FROM temp.gone_chats)")
            conn.executemany(
                "INSERT INTO ignored_chats (key, kind, name, at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET kind = excluded.kind, name = excluded.name, "
                "at = excluded.at", [(c["key"], c["kind"], c["name"], now_ms()) for c in chats])
            if files:
                used = {r[0] for r in conn.execute(
                    "SELECT json_extract(f.value, '$.sha256') FROM messages m, "
                    "json_each(m.media, '$.files') f WHERE m.media LIKE '%\"files\"%'")}
                files -= used
                if files:
                    conn.executemany("DELETE FROM media_captions WHERE sha256 = ?",
                                     [(f,) for f in files])
            return {"chats": [{k: c[k] for k in ("id", "key", "kind", "name")} for c in chats],
                    "messages": n}, files
        out, files = self.file.write_sync(run)
        folder = media_dir(self)
        for sha in files:
            (folder / sha).unlink(missing_ok=True)
        return out

    def ignored_chats(self) -> list[dict[str, Any]]:
        """Chats deleted and ignored, the latest first, with the name they had. A chat
        that had none (or was stored with its key as the name) gets its label."""
        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            out = []
            for r in conn.execute("SELECT key, kind, name, at FROM ignored_chats "
                                  "ORDER BY at DESC, key"):
                name = r["name"] if r["name"] and r["name"] != r["key"] else \
                    names.chat_label(r["key"], r["kind"])
                out.append({"key": r["key"], "kind": r["kind"], "name": name, "at": r["at"]})
            return out
        return self.file.read_sync(run)

    def unignore(self, keys: list[str]) -> list[str]:
        """Stop ignoring these chats; returns the keys that were ignored. What brings a
        chat back is the caller's (the connector)."""
        def run(conn: sqlite3.Connection) -> list[str]:
            found = [k for k in dict.fromkeys(str(k) for k in keys) if conn.execute(
                "SELECT 1 FROM ignored_chats WHERE key = ?", (k,)).fetchone()]
            conn.executemany("DELETE FROM ignored_chats WHERE key = ?", [(k,) for k in found])
            return found
        return self.file.write_sync(run)

    # ---- messages ----------------------------------------------------------------
    _MSG_SELECT = """
        SELECT m.*, p.id AS p_id, p.key AS p_key, p.phone, p.custom_name, p.contact_name,
               p.push_name, p.display_name, p.username
        FROM messages m LEFT JOIN people p ON p.id = m.sender_id
    """

    def _msg_dict(self, conn: sqlite3.Connection, r: sqlite3.Row,
                  replies: dict[str, dict] | None = None) -> dict[str, Any]:
        sender = None
        if r["p_id"] is not None and not r["from_me"]:
            sender = names.person(_RowView(r, {"id": "p_id", "key": "p_key"}))
        reply = None
        if r["reply_key"]:
            reply = (replies or {}).get(r["reply_key"])
        return {
            "id": r["id"], "ref": f"{self.id}:{r['id']}", "chat": r["chat_id"],
            "ts": r["ts"], "from_me": bool(r["from_me"]), "sender": sender,
            "text": r["text"], "kind": r["kind"], "media": media_text.parse(r["media"]),
            "reply_to": reply, "edited": bool(r["edited"]), "deleted_at": r["deleted_at"],
        }

    def _replies(self, conn: sqlite3.Connection, chat_id: int,
                 keys: list[str]) -> dict[str, dict]:
        if not keys:
            return {}
        out: dict[str, dict] = {}
        for part in in_batches(keys):
            rows = conn.execute(
                f"{self._MSG_SELECT} WHERE m.chat_id = ? AND m.key IN "
                f"({','.join('?' * len(part))})", [chat_id, *part]).fetchall()
            for r in rows:
                sender = "me" if r["from_me"] else (
                    names.resolve(_RowView(r, {"id": "p_id", "key": "p_key"}))[0]
                    if r["p_id"] is not None else None)
                out[r["key"]] = {"id": r["id"], "sender": sender,
                                 "text": (r["text"] or _media_label(r["media"]) or "")[:200]}
        return out

    def messages(self, chat_id: int, *, before: int | None = None, after: int | None = None,
                 around: int | None = None, limit: int = 100) -> dict[str, Any]:

        def anchor(conn, mid: int) -> tuple[int, int] | None:
            r = conn.execute("SELECT ts, id FROM messages WHERE id = ? AND chat_id = ?",
                             (mid, chat_id)).fetchone()
            return (r["ts"], r["id"]) if r else None

        def older(conn, key: tuple[int, int] | None, n: int, inclusive=False) -> list:
            if key is None:
                return conn.execute(f"{self._MSG_SELECT} WHERE m.chat_id = ? "
                                    f"ORDER BY m.ts DESC, m.id DESC LIMIT ?",
                                    (chat_id, n)).fetchall()[::-1]
            op = "<=" if inclusive else "<"
            return conn.execute(
                f"{self._MSG_SELECT} WHERE m.chat_id = ? AND (m.ts, m.id) {op} (?, ?) "
                f"ORDER BY m.ts DESC, m.id DESC LIMIT ?",
                (chat_id, key[0], key[1], n)).fetchall()[::-1]

        def newer(conn, key: tuple[int, int], n: int) -> list:
            return conn.execute(
                f"{self._MSG_SELECT} WHERE m.chat_id = ? AND (m.ts, m.id) > (?, ?) "
                f"ORDER BY m.ts ASC, m.id ASC LIMIT ?",
                (chat_id, key[0], key[1], n)).fetchall()

        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            if around is not None:
                a = anchor(conn, around)
                if a is None:
                    rows = older(conn, None, limit)
                else:
                    half = limit // 2
                    rows = older(conn, a, half + 1, inclusive=True) + newer(conn, a, half)
            elif before is not None:
                a = anchor(conn, before)
                rows = older(conn, a, limit) if a else []
            elif after is not None:
                a = anchor(conn, after)
                rows = newer(conn, a, limit) if a else []
            else:
                rows = older(conn, None, limit)
            has_before = has_after = False
            if rows:
                f, l = rows[0], rows[-1]
                has_before = conn.execute(
                    "SELECT 1 FROM messages WHERE chat_id = ? AND (ts, id) < (?, ?) LIMIT 1",
                    (chat_id, f["ts"], f["id"])).fetchone() is not None
                has_after = conn.execute(
                    "SELECT 1 FROM messages WHERE chat_id = ? AND (ts, id) > (?, ?) LIMIT 1",
                    (chat_id, l["ts"], l["id"])).fetchone() is not None
            replies = self._replies(conn, chat_id,
                                    list({r["reply_key"] for r in rows if r["reply_key"]}))
            msgs = [self._msg_dict(conn, r, replies) for r in rows]
            _attach_edits(conn, msgs)
            return {"messages": msgs, "has_before": has_before, "has_after": has_after}
        return self.file.read_sync(run)

    def message(self, mid: int) -> dict[str, Any] | None:
        def run(conn: sqlite3.Connection) -> dict[str, Any] | None:
            r = conn.execute(f"{self._MSG_SELECT} WHERE m.id = ?", (mid,)).fetchone()
            if r is None:
                return None
            replies = self._replies(conn, r["chat_id"], [r["reply_key"]] if r["reply_key"] else [])
            d = self._msg_dict(conn, r, replies)
            _attach_edits(conn, [d])
            c = conn.execute(f"{self._CHAT_SELECT} WHERE c.id = ?", (r["chat_id"],)).fetchone()
            d["chat"] = self._chat_row(c) if c else None
            return d
        return self.file.read_sync(run)

    def message_file(self, mid: int, sha: str) -> tuple[str | None, dict[str, Any]] | None:
        """(media type, file record) of the file `sha` in message `mid`'s `media.files`;
        None when that message has no such file."""
        row = self.file.read_sync(lambda c: c.execute(
            "SELECT media FROM messages WHERE id = ?", (mid,)).fetchone())
        media = media_text.parse(row[0]) if row else None
        if not isinstance(media, dict):
            return None
        f = next((f for f in media.get("files") or []
                  if isinstance(f, dict) and f.get("sha256") == sha), None)
        return (media.get("type"), f) if f else None

    def messages_by_ids(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        def run(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
            out: dict[int, dict[str, Any]] = {}
            for part in in_batches(ids):
                rows = conn.execute(f"{self._MSG_SELECT} WHERE m.id IN "
                                    f"({','.join('?' * len(part))})", part).fetchall()
                chat_ids = {r["chat_id"] for r in rows}
                chats = {c["id"]: self._chat_row(c) for c in conn.execute(
                    f"{self._CHAT_SELECT} WHERE c.id IN ({','.join('?' * len(chat_ids))})",
                    list(chat_ids)).fetchall()} if chat_ids else {}
                for r in rows:
                    d = self._msg_dict(conn, r)
                    ch = chats.get(r["chat_id"])
                    d["chat"] = {"id": ch["id"], "name": ch["name"], "kind": ch["kind"]} \
                        if ch else None
                    out[r["id"]] = d
            _attach_edits(conn, list(out.values()))
            return out
        return self.file.read_sync(run)

    # ---- people --------------------------------------------------------------------
    def people(self, q: str = "", limit: int = 100, offset: int = 0,
               kept: frozenset[int] = frozenset()) -> dict[str, Any]:
        """The people list: people with LISTED_MIN_MESSAGES or more, and the `kept` ones
        (in a person record), the busiest first."""
        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            where = f"p.is_me = 0 AND {listed_sql(kept)}"
            args: list[Any] = []
            if q.strip():
                like = f"%{q.strip()}%"
                digits = "".join(ch for ch in q if ch.isdigit())
                where += (" AND (p.custom_name LIKE ? OR p.contact_name LIKE ? OR "
                          "p.push_name LIKE ? OR p.display_name LIKE ? OR p.username LIKE ?"
                          + (" OR p.phone LIKE ?" if len(digits) >= 3 else "") + ")")
                args += [like] * 5 + ([f"%{digits}%"] if len(digits) >= 3 else [])
            rows = conn.execute(
                f"""SELECT p.*, COALESCE(s.n, 0) AS n, COALESCE(s.chats, 0) AS nchats
                    FROM people p
                    LEFT JOIN (SELECT sender_id, COUNT(*) AS n, COUNT(DISTINCT chat_id) AS chats
                               FROM messages WHERE sender_id IS NOT NULL GROUP BY sender_id) s
                           ON s.sender_id = p.id
                    WHERE {where}
                    ORDER BY n DESC, p.id LIMIT ? OFFSET ?""",
                [*args, limit, offset]).fetchall()
            return {"people": [self.person_dict(r) for r in rows]}
        return self.file.read_sync(run)

    def unused_people(self, keep: set[int], *, without_chats: set[int] = frozenset()
                      ) -> list[int]:
        """People nothing refers to: no message of theirs and no dm with them (chats in
        `without_chats` do not count), not you, no name you gave them, not in `keep`
        (the ones in a person record)."""
        def run(conn: sqlite3.Connection) -> list[int]:
            drop = json.dumps(sorted(without_chats))
            return [r[0] for r in conn.execute(
                """SELECT p.id FROM people p
                   WHERE p.is_me = 0 AND NULLIF(TRIM(p.custom_name), '') IS NULL
                     AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.sender_id = p.id
                                     AND m.chat_id NOT IN (SELECT value FROM json_each(?)))
                     AND NOT EXISTS (SELECT 1 FROM chats c WHERE c.person_id = p.id
                                     AND c.id NOT IN (SELECT value FROM json_each(?)))""",
                (drop, drop)) if r[0] not in keep]
        return self.file.read_sync(run)

    def delete_people(self, ids: list[int]) -> int:
        """Delete people rows (from `unused_people`). A row something still refers to is
        refused by its foreign key."""
        def run(conn: sqlite3.Connection) -> int:
            n = 0
            for part in in_batches(ids):
                n += conn.execute(f"DELETE FROM people WHERE id IN ({','.join('?' * len(part))})"
                                  " AND is_me = 0", part).rowcount
            return n
        return self.file.write_sync(run)

    def person_dict(self, r: sqlite3.Row) -> dict[str, Any]:
        base = names.person(r) or {}
        base["names"] = {"custom": r["custom_name"], "contact": r["contact_name"],
                         "push": r["push_name"], "display": r["display_name"],
                         "username": r["username"]}
        base["messages"] = r["n"] if "n" in r.keys() else None
        base["chats"] = r["nchats"] if "nchats" in r.keys() else None
        return base

    def update_person(self, pid: int, name: str | None) -> dict | None:
        def run(conn: sqlite3.Connection):
            conn.execute("UPDATE people SET custom_name = ? WHERE id = ?", (_clean(name), pid))
            return conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()
        r = self.file.write_sync(run)
        return self.person_dict(r) if r else None

    # ---- search --------------------------------------------------------------------
    # Instant search runs on the normalised shadow index (kb/instant.py); the ix_*
    # methods are what instant.run calls per knowledge base.

    @property
    def search_ready(self) -> bool:
        return self.indexer.ready

    def ix_filters(self, query: Query, *, chat: int | None = None,
                   people: list[int] | None = None, ts_from: int | None = None,
                   ts_to: int | None = None) -> instant.Filters | None:
        """The query's filters as ids of this knowledge base; None when they exclude it.
        `people`: senders (the accounts of one person) the search keeps to."""
        if query.kbs and self.id not in query.kbs:
            return None
        chats = query.refs_in(self.id, "chats")
        if chat is not None:
            chats = [chat] if chats is None or chat in chats else []
        senders = query.refs_in(self.id, "people")
        if people is not None:
            senders = people if senders is None else [x for x in senders if x in people]
        lo = max([x for x in (ts_from, query.ts_from) if x is not None], default=None)
        hi = min([x for x in (ts_to, query.ts_to) if x is not None], default=None)
        trashed = self.file.read_sync(lambda c: [r[0] for r in c.execute(
            "SELECT id FROM chats WHERE trashed = 1")])
        return instant.Filters(chats=chats, people=senders, ts_from=lo, ts_to=hi,
                               trashed=trashed)

    def ix_catch_up(self) -> None:
        """Index what an import wrote since the last search (a few thousand rows at
        most inline; more goes to the background worker)."""
        if not self.search_ready:
            return
        dirty = self.file.read_sync(
            lambda c: c.execute("SELECT 1 FROM search_dirty LIMIT 1").fetchone())
        if dirty:
            self.indexer.catch_up()

    def ix_run(self, p: instant.Plan, *, sort: str, limit: int, cursor: int | None,
               inclusive: bool, count_cap: int | None) -> dict[str, Any]:
        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            try:
                if sort == "best":
                    rows = instant.best(conn, p, limit=limit, pool=max(200, limit))
                else:
                    rows = instant.page(conn, p, sort=sort, limit=limit, cursor=cursor,
                                        inclusive=inclusive)
                if rows and len(rows) < limit and cursor is None:
                    total = len(rows)
                    capped = count_cap is not None and total > count_cap
                    total = min(total, count_cap) if capped else total
                else:
                    total, capped = instant.count(conn, p, count_cap)
            except sqlite3.OperationalError as e:
                if not is_query_error(e):
                    raise
                return {"rows": [], "total": 0, "capped": False}
            return {"rows": rows, "total": total, "capped": capped}
        return self.file.read_sync(run)

    def ix_best(self, p: instant.Plan, n: int) -> list[tuple[int, float]]:
        def run(conn: sqlite3.Connection) -> list[tuple[int, float]]:
            return instant.best(conn, p, limit=n)
        return self.file.read_sync(run)

    def ix_vocab(self):
        return self.file.read_sync(
            lambda c: self.indexer.vocab.get(c, instant.generation(c)))

    def ix_hydrate(self, rows: list[tuple[int, float]], p: instant.Plan) -> list[dict[str, Any]]:
        """Search hits for (sk, score) rows, in that order."""
        if not rows:
            return []
        score = dict(rows)

        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            sks = [sk for sk, _ in rows]
            got = {r["sk"]: r for r in conn.execute(
                f"""SELECT s.sk, m.id, m.ts, m.from_me, m.chat_id, m.sender_id, m.text,
                           m.kind, m.media, m.edited, m.deleted_at
                    FROM search_map s JOIN messages m ON m.id = s.id
                    WHERE s.sk IN ({','.join('?' * len(sks))})""", sks).fetchall()}
            chats, people = self._hit_names(conn, {r["chat_id"] for r in got.values()},
                                            {r["sender_id"] for r in got.values()
                                             if r["sender_id"] is not None})
            out = []
            for sk in sks:
                r = got.get(sk)
                if r is None:
                    continue
                text = r["text"] or _media_text(r["media"]) or ""
                out.append({
                    "ref": f"{self.id}:{r['id']}", "kb": self.id, "id": r["id"],
                    "ts": r["ts"], "sk": sk, "chat": chats.get(r["chat_id"]),
                    "sender": None if r["from_me"] else people.get(r["sender_id"]),
                    "from_me": bool(r["from_me"]), "kind": r["kind"],
                    "media": (media_text.parse(r["media"]) or {}).get("type") if r["media"] else None,
                    "snippet": highlight(text, p.exact, p.prefixes),
                    "edited": bool(r["edited"]),
                    "deleted": r["deleted_at"] is not None,
                    "score": round(score.get(sk, 0.0), 4),
                })
            return out
        return self.file.read_sync(run)

    def _hit_names(self, conn, chat_ids: set[int], person_ids: set[int]):
        chats = chat_cards(conn, chat_ids)
        people: dict[int, dict] = {}
        if person_ids:
            for pr in conn.execute(
                    f"SELECT * FROM people WHERE id IN ({','.join('?' * len(person_ids))})",
                    list(person_ids)).fetchall():
                pd = names.person(pr) or {}
                people[pr["id"]] = {"id": pr["id"], "name": pd.get("name")}
        return chats, people

    def facets(self, p: instant.Plan, top: int = 10) -> dict[str, Any]:
        """Counts per chat, person and month over a plan's matches, with names."""
        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            try:
                f = instant.facets(conn, p, top=top)
            except sqlite3.OperationalError as e:
                if not is_query_error(e):
                    raise
                return {"total": 0, "approximate": False, "chats": [], "people": [],
                        "months": []}
            chats, people = self._hit_names(conn, {c for c, _ in f["chats"]},
                                            {x for x, _ in f["people"] if x != -1})
            f["chats"] = [{"ref": f"{self.id}:{c}", "kb": self.id, "id": c,
                           "name": (chats.get(c) or {}).get("name"),
                           "kind": (chats.get(c) or {}).get("kind"),
                           "parent": (chats.get(c) or {}).get("parent"), "count": n}
                          for c, n in f["chats"]]
            f["people"] = [{"ref": None if x == -1 else f"{self.id}:{x}", "kb": self.id,
                            "id": x, "me": x == -1,
                            "name": "me" if x == -1 else (people.get(x) or {}).get("name"),
                            "count": n} for x, n in f["people"]]
            f["months"] = [{"month": f"{ym // 100:04d}-{ym % 100:02d}", "count": n}
                           for ym, n in f["months"]]
            return f
        return self.file.read_sync(run)

    def search_unindexed(self, q: str, *, chat: int | None = None,
                          people: list[int] | None = None,
                          ts_from: int | None = None, ts_to: int | None = None,
                          sort: str = "recent", limit: int = 50,
                          offset: int = 0) -> dict[str, Any]:
        """Plain FTS over messages_fts, while the instant index is (re)built in the
        background. `sort`: recent (newest first), oldest, relevance (bm25)."""
        candidates = fts_candidates(q)
        if not candidates:
            return {"hits": [], "total": 0}

        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            for match in candidates:
                try:
                    return run_one(conn, match)
                except sqlite3.OperationalError as e:
                    # a half-typed FTS expression is not an error worth showing
                    if not is_query_error(e):
                        raise
            return {"hits": [], "total": 0}

        def run_one(conn: sqlite3.Connection, match: str) -> dict[str, Any]:
            where = ["messages_fts MATCH ?"]
            args: list[Any] = [match]
            if chat is not None:
                where.append("m.chat_id = ?"); args.append(chat)
            if people is not None:
                where.append(f"m.sender_id IN ({','.join('?' * len(people)) or 'NULL'})")
                args += people
            if ts_from is not None:
                where.append("m.ts >= ?"); args.append(ts_from)
            if ts_to is not None:
                where.append("m.ts < ?"); args.append(ts_to)
            where.append("c.trashed = 0")
            w = " AND ".join(where)
            order = {"relevance": "rank", "oldest": "m.ts ASC, m.id ASC"}.get(
                sort, "m.ts DESC, m.id DESC")
            total = conn.execute(
                f"SELECT COUNT(*) FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
                f"JOIN chats c ON c.id = m.chat_id WHERE {w}", args).fetchone()[0]
            rows = conn.execute(
                f"""SELECT m.id, m.ts, m.from_me, m.chat_id, m.sender_id,
                           snippet(messages_fts, 0, '{_MARK_A}', '{_MARK_B}', '…', 24) AS snip,
                           bm25(messages_fts) AS score
                    FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
                    JOIN chats c ON c.id = m.chat_id
                    WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?""",
                [*args, limit, offset]).fetchall()
            chats, senders = self._hit_names(
                conn, {r["chat_id"] for r in rows},
                {r["sender_id"] for r in rows if r["sender_id"] is not None})
            hits = [{
                "ref": f"{self.id}:{r['id']}", "kb": self.id, "id": r["id"], "ts": r["ts"],
                "chat": chats.get(r["chat_id"]),
                "sender": None if r["from_me"] else senders.get(r["sender_id"]),
                "from_me": bool(r["from_me"]),
                "snippet": snippet_html(r["snip"] or ""),
                "score": r["score"],
            } for r in rows]
            return {"hits": hits, "total": total}
        return self.file.read_sync(run)

    # ---- numbers -------------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            r = conn.execute(
                f"""SELECT (SELECT COUNT(*) FROM chats WHERE message_count > 0) AS chats,
                          (SELECT COALESCE(SUM(message_count), 0) FROM chats) AS messages,
                          (SELECT COUNT(*) FROM people p JOIN
                             (SELECT sender_id, COUNT(*) AS n FROM messages
                              WHERE sender_id IS NOT NULL GROUP BY sender_id) s
                             ON s.sender_id = p.id
                           WHERE p.is_me = 0 AND {listed_sql()}) AS people,
                          (SELECT MIN(first_ts) FROM chats) AS first_ts,
                          (SELECT MAX(last_ts) FROM chats) AS last_ts""").fetchone()
            # the semantic index (kb/passages.py): passages, embedded, ready
            sem = P.status(conn)
            return {"id": self.id, "kind": self.kind, "title": self.title,
                    "platform": self.platform, "chats": r["chats"], "messages": r["messages"],
                    "people": r["people"], "first_ts": r["first_ts"], "last_ts": r["last_ts"],
                    "semantic": sem}
        return self.file.read_sync(run)


def _chat_name(r: sqlite3.Row) -> str:
    """A row of ChatKB._CHAT_SELECT -> the chat's name as every list shows it."""
    if r["kind"] == "dm" and r["p_id"] is not None and not r["custom_name"]:
        # names.resolve wants people columns under their own names
        return names.resolve(_RowView(r, {"custom_name": "p_custom", "key": "key_p"}))[0]
    if r["display"] == r["key"]:                 # no name anywhere: never the raw key
        return names.chat_label(r["key"], r["kind"])
    return r["display"]


def chat_cards(conn: sqlite3.Connection, chat_ids: set[int]) -> dict[int, dict[str, Any]]:
    """{id: {id, name, kind, parent}} of these chats."""
    ids = sorted(chat_ids)
    out: dict[int, dict[str, Any]] = {}
    for part in in_batches(ids):
        for r in conn.execute(f"{ChatKB._CHAT_SELECT} WHERE c.id IN "
                              f"({','.join('?' * len(part))})", part):
            out[r["id"]] = {"id": r["id"], "name": _chat_name(r), "kind": r["kind"],
                            "parent": r["parent"]}
    return out


class _RowView:
    """A sqlite Row with some columns read under another name, so names.resolve
    can take joined rows without every query aliasing people columns the same way."""

    def __init__(self, row: sqlite3.Row, alias: dict[str, str]):
        self.row = row
        self.alias = alias

    def keys(self):
        return set(self.row.keys()) | set(self.alias)

    def __getitem__(self, k: str):
        return self.row[self.alias.get(k, k)]


_MEDIA_LABEL = {"photo": "Photo", "video": "Video", "voice": "Voice message",
                "audio": "Audio", "document": "Document", "sticker": "Sticker",
                "gif": "GIF", "location": "Location", "contact": "Contact",
                "file": "File", "call": "Call"}


def _attach_edits(conn: sqlite3.Connection, msgs: list[dict[str, Any]]) -> None:
    """`edits`: the earlier texts of each message, oldest first ([] when never edited
    or when the edit was not seen: WhatsApp backups only say `edited`)."""
    for m in msgs:
        m["edits"] = []
    ids = [m["id"] for m in msgs]
    if not ids:
        return
    by_id = {m["id"]: m for m in msgs}
    for part in in_batches(ids):
        for r in conn.execute(
                f"SELECT message_id, ts, old_text FROM message_edits WHERE message_id IN "
                f"({','.join('?' * len(part))}) ORDER BY ts, id", part):
            by_id[r[0]]["edits"].append({"ts": r[1], "text": r[2]})


def _media_text(raw: str | None) -> str | None:
    """Words a media message can be found by when it has no text: its file name."""
    m = media_text.parse(raw)
    return (m.get("caption") or m.get("name")) if m else None


def _media_label(raw: str | None) -> str | None:
    m = media_text.parse(raw)
    if not m:
        return None
    return m.get("caption") or _MEDIA_LABEL.get(m.get("type", ""), "Attachment")
