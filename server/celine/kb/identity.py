"""People across sources: person records in celine.db.

A person record (tables `identities`, `identity_members`, `person_notes`) holds accounts
(`kb:person:id`), a name, `relation` (how the person relates to you: "my sister"),
`aliases` (other names they go by) and notes about the person, each written by you or by
the agent. An account is in at most one record.
It lives in celine.db because a knowledge base is a file and nothing joins across files.

- Two or more accounts are a **same person**: search, the People page and Celine treat
  them as one person. Only you, or Celine when you ask it, mark accounts (`link_sync`).
- A record with one account exists only for its relation, aliases or notes; it is not a
  same person, and its name is empty.

Nothing is merged in the archives: every message keeps its sender. Everything here is
sync on a sqlite connection, run through `Store.read` / `Store.write`, plus `snapshot`,
an in-memory copy that search and retrieval read without touching the database per
lookup.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from ..core.ids import now
from . import names

# References, one shape per kind. Chats, people and messages each count from 1 in a
# source, so the kind is part of the reference:
#   a chat "whatsapp:chat:213", a person (an account) "whatsapp:person:17", a message
#   "whatsapp:msg:48213". A piece of a document is "documents:812": Documents has one kind.
_KB = r"([a-z0-9][a-z0-9_-]*)"
CHAT_REF = re.compile(rf"^{_KB}:chat:(\d+)$")
PERSON_REF = re.compile(rf"^{_KB}:person:(\d+)$")
MSG_REF = re.compile(rf"^{_KB}:msg:(\d+)$")
SEGMENT_REF = re.compile(rf"^{_KB}:(\d+)$")
KINDS = ("chat", "person", "msg")       # no source may have one of these as its id
NOTE_MAX = 2000
RELATION_MAX = 120
ALIASES_MAX = 20
ALIAS_MAX = 60


class IdentityError(ValueError):
    pass


def chat_ref(kb: str, chat_id: int) -> str:
    return f"{kb}:chat:{chat_id}"


def person_ref(kb: str, person_id: int) -> str:
    return f"{kb}:person:{person_id}"


def msg_ref(kb: str, message_id: int) -> str:
    return f"{kb}:msg:{message_id}"


def kind_of(value: str) -> str | None:
    """"chat", "person" or "message" by the shape of a reference; None for anything
    else (a name, a document piece)."""
    v = str(value).strip().strip("[]")
    return ("chat" if CHAT_REF.match(v) else "person" if PERSON_REF.match(v)
            else "message" if MSG_REF.match(v) else None)


_WORDS = {"chat": "a chat", "person": "a person", "message": "a message"}
_EXAMPLE = {"chat": "whatsapp:chat:213", "person": "whatsapp:person:17",
            "message": "whatsapp:msg:48213"}


def not_a(value: str, want: str) -> str:
    """One sentence for a value passed where a `want` ("chat", "person", "message")
    reference belongs: what it is, and the shape that belongs there."""
    kind = kind_of(value)
    is_ = f"{value} is {_WORDS[kind]}" if kind else f"{value!r} is no reference"
    return f"{is_}, not {_WORDS[want]}. {_WORDS[want].capitalize()} reference looks like " \
           f"{_EXAMPLE[want]}."


def parse_person(ref: str) -> tuple[str, int]:
    m = PERSON_REF.match(str(ref).strip())
    if not m:
        raise IdentityError(not_a(str(ref), "person"))
    return m[1], int(m[2])


def parse_chat(ref: str) -> tuple[str, int] | None:
    m = CHAT_REF.match(str(ref).strip())
    return (m[1], int(m[2])) if m else None


def parse_msg(ref: str) -> tuple[str, int] | None:
    m = MSG_REF.match(str(ref).strip().strip("[]"))
    return (m[1], int(m[2])) if m else None


def norm_name(s: str | None) -> str:
    """Casefolded, accents and emoji removed, whitespace collapsed. 'Älex ✨' -> 'alex'."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s.casefold().replace("ß", "ss"))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if (c.isalnum() or c.isspace()) else " " for c in s)
    return " ".join(s.split())


# ---- snapshot -----------------------------------------------------------------------

@dataclass
class Identity:
    id: int
    name: str
    members: set[str] = field(default_factory=set)
    relation: str = ""
    aliases: list[str] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)   # id, text, author, created_at

    @property
    def same_person(self) -> bool:
        return len(self.members) > 1


@dataclass
class Identities:
    by_id: dict[int, Identity] = field(default_factory=dict)
    of_ref: dict[str, int] = field(default_factory=dict)

    def expand(self, ref: str) -> set[str]:
        """Every account that is the same person as `ref` (itself included)."""
        iid = self.of_ref.get(ref)
        return set(self.by_id[iid].members) if iid is not None else {ref}

    def identity_of(self, ref: str) -> Identity | None:
        """The record `ref` is in: a same person, or what is kept of a single account."""
        iid = self.of_ref.get(ref)
        return self.by_id.get(iid) if iid is not None else None

    def same_person(self, ref: str) -> Identity | None:
        """The record of `ref` when it is a same person (two or more accounts)."""
        i = self.identity_of(ref)
        return i if i is not None and i.same_person else None

    def named(self, name: str) -> list[Identity]:
        """Records `name` names: a same person by its name, any record by an alias."""
        n = norm_name(name)
        if not n:
            return []
        return [i for i in self.by_id.values()
                if (i.same_person and norm_name(i.name) == n)
                or any(norm_name(a) == n for a in i.aliases)]

    def refs(self) -> set[str]:
        """Every account in a record."""
        return set(self.of_ref)


def load_sync(conn: sqlite3.Connection) -> Identities:
    out = Identities()
    for r in conn.execute("SELECT id, name, relation, aliases FROM identities"):
        out.by_id[r[0]] = Identity(r[0], r[1], relation=r[2], aliases=json.loads(r[3]))
    for r in conn.execute("SELECT id, identity_id, text, author, created_at FROM "
                          "person_notes WHERE replaced_at IS NULL ORDER BY id"):
        if r[1] in out.by_id:
            out.by_id[r[1]].notes.append({"id": r[0], "text": r[2], "author": r[3],
                                          "created_at": r[4]})
    for r in conn.execute("SELECT kb, person_id, identity_id FROM identity_members"):
        ref = person_ref(r[0], r[1])
        if r[2] in out.by_id:
            out.by_id[r[2]].members.add(ref)
            out.of_ref[ref] = r[2]
    return out


async def snapshot(store) -> Identities:
    if store is None:
        return Identities()
    return await store.read(load_sync)


# ---- people from the knowledge bases -------------------------------------------------

_ACCOUNT_SQL = """SELECT p.*, COALESCE(s.n, 0) AS n, COALESCE(s.chats, 0) AS nchats, s.last_ts
    FROM people p
    LEFT JOIN (SELECT sender_id, COUNT(*) AS n, COUNT(DISTINCT chat_id) AS chats,
                      MAX(ts) AS last_ts
               FROM messages WHERE sender_id IS NOT NULL GROUP BY sender_id) s
           ON s.sender_id = p.id
    WHERE p.is_me = 0 AND {where} ORDER BY n DESC, p.id LIMIT ?"""


def _account_rows(kb, where: str, args: list[Any], limit: int) -> list[dict]:
    """Accounts of one chat kb as the People page shows them: the person as in
    `GET /api/sources/{sid}/people` plus `ref`, `kb`, `handle` and `last_ts`."""
    sql = _ACCOUNT_SQL.format(where=where)
    rows = kb.file.read_sync(lambda c: c.execute(sql, [*args, limit]).fetchall())
    out = []
    for r in rows:
        d = kb.person_dict(r)
        out.append({**d, "ref": person_ref(kb.id, r["id"]), "kb": kb.id,
                    "handle": _handle(r, d["phone"]), "last_ts": r["last_ts"]})
    return out


def _handle(r: sqlite3.Row, phone: str | None) -> str | None:
    """What tells an account apart beyond the name: the number (WhatsApp; a
    `<number>@s.whatsapp.net` id is the number), the @username (Discord), else None."""
    if phone:
        return phone
    if r["key"].endswith("@s.whatsapp.net"):
        return names.format_phone("+" + r["key"].split("@")[0].split(":")[0])
    return f"@{r['username']}" if r["username"] else None


def accounts(kbs, refs) -> dict[str, dict]:
    """ref -> the account, as the People page shows it. Unknown refs are left out.
    `kbs`: the knowledge bases, or a list of them."""
    from .chat import ChatKB
    get = kbs.get if hasattr(kbs, "get") else {k.id: k for k in kbs}.get
    by_kb: dict[str, list[int]] = {}
    for ref in refs:
        k, pid = parse_person(ref)
        by_kb.setdefault(k, []).append(pid)
    out: dict[str, dict] = {}
    for k, ids in by_kb.items():
        kb = get(k)
        if isinstance(kb, ChatKB):
            marks = ",".join("?" * len(ids))
            for a in _account_rows(kb, f"p.id IN ({marks})", ids, len(ids)):
                out[a["ref"]] = a
    return out


def person(kbs, ids: "Identities", ref: str) -> dict[str, Any] | None:
    """One person in full, for the People page: what is kept about them and every
    account with its profile and the names it had. None: no such account."""
    accts = accounts(kbs, ids.expand(ref))
    if not accts:
        return None
    for a in accts.values():
        a["history"] = kbs.get(a["kb"]).name_history([a["id"]])[a["id"]]
    rec = ids.identity_of(ref)
    group = sorted(accts.values(), key=lambda a: -(a["messages"] or 0))
    return {"person": rec.id if rec else None,
            "same_person": bool(rec and rec.same_person),
            "name": rec.name if rec and rec.same_person else group[0]["name"],
            "relation": rec.relation if rec else "",
            "aliases": rec.aliases if rec else [], "notes": rec.notes if rec else [],
            "accounts": group}


def _search_accounts(kbs, q: str, kb_id: str | None, limit: int,
                     kept: set[str]) -> list[dict]:
    """Accounts by the people lists' rule (kb/chat.py `listed_sql`: enough messages, or
    in a record), filtered by name, number or user name, busiest first."""
    from .chat import listed_sql
    where, args = "1", []
    if q.strip():
        like = f"%{q.strip()}%"
        digits = "".join(ch for ch in q if ch.isdigit())
        # what the People page shows: names, and the number of a phone handle (a phone
        # field or a number@s.whatsapp.net key); never a raw id nobody sees
        where = ("(p.custom_name LIKE ? OR p.contact_name LIKE ? OR p.push_name LIKE ? "
                 "OR p.display_name LIKE ? OR p.username LIKE ?"
                 + (" OR p.phone LIKE ? OR (p.key LIKE '%@s.whatsapp.net' AND p.key LIKE ?)"
                    if len(digits) >= 3 else "") + ")")
        args = [like] * 5 + ([f"%{digits}%"] * 2 if len(digits) >= 3 else [])
    out: list[dict] = []
    for kb in kbs.chats():
        if kb_id and kb.id != kb_id:
            continue
        mine = {pid for k, pid in map(parse_person, kept) if k == kb.id}
        out += _account_rows(kb, f"{where} AND {listed_sql(mine)}", args, limit)
    return out


def people(kbs, ids: Identities, q: str = "", kb_id: str | None = None,
           limit: int = 500) -> list[dict]:
    """The People page: one entry per person, the busiest first. A same person is one
    entry with its accounts inside; every other account is an entry of its own. `q`
    matches account names, the number and the user name, and a record's name and
    aliases; a match on one account brings its whole person. `kb_id` keeps the people
    with an account there.

    Entry: `key` ("person:3" or the account's ref), `person` (the record id or None),
    `same_person`, `name`, `relation`, `aliases`, `notes`, `messages`, `last_ts`,
    `accounts`."""
    found = _search_accounts(kbs, q, kb_id, limit, ids.refs())
    by_ref = {a["ref"]: a for a in found}
    wanted = {ids.of_ref[r] for r in by_ref if r in ids.of_ref}
    n = norm_name(q)
    if n:
        wanted |= {i.id for i in ids.by_id.values()
                   if (i.same_person and n in norm_name(i.name))
                   or any(n in norm_name(a) for a in i.aliases)}
    if kb_id:
        wanted = {w for w in wanted
                  if any(parse_person(m)[0] == kb_id for m in ids.by_id[w].members)}
    missing = [m for w in wanted for m in ids.by_id[w].members if m not in by_ref]
    by_ref.update(accounts(kbs, missing))

    out: list[dict] = []
    done: set[int] = set()
    for ref, a in by_ref.items():
        rec = ids.identity_of(ref)
        if rec is None:
            out.append(_entry(None, [a]))
        elif rec.id in wanted and rec.id not in done:
            done.add(rec.id)
            members = [by_ref[m] for m in rec.members if m in by_ref]
            out.append(_entry(rec, sorted(members, key=lambda x: (-(x["messages"] or 0),
                                                                  x["ref"]))))
    out.sort(key=lambda e: (-e["messages"], e["key"]))
    return out[:limit]


def _entry(rec: Identity | None, accts: list[dict]) -> dict:
    same = rec is not None and rec.same_person
    return {"key": f"person:{rec.id}" if same else accts[0]["ref"],
            "person": rec.id if rec else None, "same_person": same,
            "name": rec.name if same else accts[0]["name"],
            "relation": rec.relation if rec else "",
            "aliases": rec.aliases if rec else [], "notes": rec.notes if rec else [],
            "messages": sum(a["messages"] or 0 for a in accts),
            "last_ts": max((a["last_ts"] for a in accts if a["last_ts"]), default=None),
            "accounts": accts}


def group_counts(rows: list[dict], ids: Identities) -> list[dict]:
    """Search facet rows ({ref, kb, id, name, me, count}) with the accounts of one same
    person summed into one row named after the person, most first. Its ref is the
    person's first account; search expands it to all of them."""
    out: dict[str, dict] = {}
    for r in rows:
        rec = ids.same_person(r["ref"]) if r["ref"] else None
        if rec is None:
            out[r["ref"] or f"me:{r['kb']}"] = dict(r)
            continue
        key = f"person:{rec.id}"
        if key in out:
            out[key]["count"] += r["count"]
            continue
        first = min(rec.members)
        k, pid = parse_person(first)
        out[key] = {**r, "ref": first, "kb": k, "id": pid, "name": rec.name}
    return sorted(out.values(), key=lambda x: -x["count"])


# ---- reads and writes on celine.db ---------------------------------------------------

def kept_sync(conn: sqlite3.Connection, kb: str | None = None) -> set[str]:
    """Every account in a record ("whatsapp:person:88"), of one source or all: a same
    person, or an account with a relation, aliases or notes. The people lists show them
    whatever their message count, and the purge keeps them."""
    return {person_ref(r[0], r[1]) for r in conn.execute(
        "SELECT kb, person_id FROM identity_members WHERE ? IS NULL OR kb = ?", (kb, kb))}


def _notes(conn: sqlite3.Connection, iid: int) -> list[dict[str, Any]]:
    return [{"id": r[0], "text": r[1], "author": r[2], "created_at": r[3]}
            for r in conn.execute("SELECT id, text, author, created_at FROM person_notes "
                                  "WHERE identity_id = ? AND replaced_at IS NULL ORDER BY id",
                                  (iid,))]


def _record(conn: sqlite3.Connection, iid: int) -> dict[str, Any]:
    row = conn.execute("SELECT id, name, relation, aliases FROM identities WHERE id = ?",
                       (iid,)).fetchone()
    members = sorted(person_ref(r[0], r[1]) for r in conn.execute(
        "SELECT kb, person_id FROM identity_members WHERE identity_id = ?", (iid,)))
    return {"id": row[0], "name": row[1], "relation": row[2], "aliases": json.loads(row[3]),
            "notes": _notes(conn, iid), "members": members}


def _record_of(conn: sqlite3.Connection, k: str, pid: int) -> int | None:
    row = conn.execute("SELECT identity_id FROM identity_members WHERE kb = ? AND "
                       "person_id = ?", (k, pid)).fetchone()
    return row[0] if row else None


def _clean_text(text: str, most: int, what: str) -> str:
    text = (text or "").strip()
    if len(text) > most:
        raise IdentityError(f"{what} is too long. Keep it under {most} characters.")
    return text


def _clean_aliases(items: list[str]) -> list[str]:
    """Trimmed, one of each (by norm_name), in the order given."""
    out, seen = [], set()
    for a in items:
        a = " ".join(str(a).split())
        n = norm_name(a)
        if not n or n in seen:
            continue
        if len(a) > ALIAS_MAX:
            raise IdentityError(f"The alias {a[:20]!r}... is too long. Keep each alias "
                                f"under {ALIAS_MAX} characters.")
        seen.add(n)
        out.append(a)
    if len(out) > ALIASES_MAX:
        raise IdentityError(f"A person has too many aliases. Keep at most {ALIASES_MAX}.")
    return out


def _holds(rec: dict[str, Any]) -> bool:
    """Does a record say anything of its own (beyond linking accounts)?"""
    return bool(rec["relation"] or rec["aliases"] or rec["notes"])


def _empty(ref: str) -> dict[str, Any]:
    return {"id": None, "name": "", "relation": "", "aliases": [], "notes": [],
            "members": [ref]}


def _ensure(conn: sqlite3.Connection, ref: str) -> int:
    """The record of account `ref`, made when it has none."""
    k, pid = parse_person(ref)
    iid = _record_of(conn, k, pid)
    if iid is None:
        at = now()
        iid = conn.execute("INSERT INTO identities (name, created_at, updated_at) "
                           "VALUES ('', ?, ?)", (at, at)).lastrowid
        conn.execute("INSERT INTO identity_members (kb, person_id, identity_id, source, "
                     "created_at) VALUES (?, ?, ?, 'user', ?)", (k, pid, iid, at))
    return iid


def _settle(conn: sqlite3.Connection, iid: int, ref: str) -> dict[str, Any]:
    """The record after a change: a single account's record that says nothing is
    deleted."""
    rec = _record(conn, iid)
    if len(rec["members"]) < 2 and not _holds(rec):
        conn.execute("DELETE FROM identities WHERE id = ?", (iid,))
        return _empty(ref)
    return rec


def link_sync(conn: sqlite3.Connection, refs: list[str], *, name: str,
              source: str = "user") -> dict[str, Any]:
    """Mark `refs` as the same person called `name`. Accounts already in records bring
    them along: the records merge into one, with their notes and aliases."""
    parsed = list(dict.fromkeys(parse_person(r) for r in refs))
    name = (name or "").strip()
    if not name:
        raise IdentityError("The same person needs a name. Give one.")
    at = now()
    records = sorted({r for r in (_record_of(conn, k, pid) for k, pid in parsed)
                      if r is not None})
    if records:
        sizes = {iid: conn.execute("SELECT COUNT(*) FROM identity_members WHERE "
                                   "identity_id = ?", (iid,)).fetchone()[0]
                 for iid in records}
        iid = max(records, key=lambda i: (sizes[i], -i))
        merged = [_record(conn, i) for i in [iid] + [i for i in records if i != iid]]
        relation = next((r["relation"] for r in merged if r["relation"]), "")
        aliases = _clean_aliases([a for r in merged for a in r["aliases"]])
        for other in merged[1:]:
            conn.execute("UPDATE identity_members SET identity_id = ? WHERE identity_id = ?",
                         (iid, other["id"]))
            conn.execute("UPDATE person_notes SET identity_id = ? WHERE identity_id = ?",
                         (iid, other["id"]))
            conn.execute("DELETE FROM identities WHERE id = ?", (other["id"],))
        conn.execute("UPDATE identities SET name = ?, relation = ?, aliases = ?, "
                     "updated_at = ? WHERE id = ?",
                     (name, relation, json.dumps(aliases), at, iid))
    else:
        iid = conn.execute("INSERT INTO identities (name, created_at, updated_at) "
                           "VALUES (?, ?, ?)", (name, at, at)).lastrowid
    for k, pid in parsed:
        conn.execute("INSERT INTO identity_members (kb, person_id, identity_id, source, "
                     "created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (kb, person_id) "
                     "DO NOTHING", (k, pid, iid, source, at))
    out = _record(conn, iid)
    if len(out["members"]) < 2:
        raise IdentityError("Pick two or more accounts to mark as the same person.")
    return out


def unlink_sync(conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    """Separate one account from its same person. The record stays with the accounts
    left; one account left keeps it only for what it says, else it is deleted."""
    k, pid = parse_person(ref)
    iid = _record_of(conn, k, pid)
    rec = _record(conn, iid) if iid is not None else None
    if rec is None or len(rec["members"]) < 2:
        raise IdentityError(f"{ref} is not marked as the same person as another account.")
    conn.execute("DELETE FROM identity_members WHERE kb = ? AND person_id = ?", (k, pid))
    if len(rec["members"]) > 2:
        conn.execute("UPDATE identities SET updated_at = ? WHERE id = ?", (now(), iid))
        return {"person": iid, "deleted": False}
    if _holds(rec):
        conn.execute("UPDATE identities SET name = '', updated_at = ? WHERE id = ?",
                     (now(), iid))
        return {"person": iid, "deleted": False}
    conn.execute("DELETE FROM identities WHERE id = ?", (iid,))
    return {"person": iid, "deleted": True}


def set_person_sync(conn: sqlite3.Connection, ref: str, *, name: str | None = None,
                    relation: str | None = None,
                    aliases: list[str] | None = None) -> dict[str, Any]:
    """Change the person the account `ref` belongs to; None leaves a field as it is.
    `name` is a same person's name (a single account is renamed in its archive).
    `relation`: how they relate to the user, one line. Returns the record, or one with
    `id` None for a single account with nothing kept."""
    k, pid = parse_person(ref)
    relation = None if relation is None else " ".join(
        _clean_text(relation, RELATION_MAX, "The relation").split())
    aliases = None if aliases is None else _clean_aliases(aliases)
    iid = _record_of(conn, k, pid)
    rec = _record(conn, iid) if iid is not None else None
    if name is not None:
        if rec is None or len(rec["members"]) < 2:
            raise IdentityError("Only a same person has a name here. Rename the account "
                                "itself instead.")
        if not name.strip():
            raise IdentityError("The same person needs a name. Give one.")
    if rec is None and not relation and not aliases:
        return _empty(ref)
    iid = _ensure(conn, ref)
    rec = _record(conn, iid)
    conn.execute("UPDATE identities SET name = ?, relation = ?, aliases = ?, updated_at = ? "
                 "WHERE id = ?",
                 (name.strip() if name is not None else rec["name"],
                  relation if relation is not None else rec["relation"],
                  json.dumps(aliases if aliases is not None else rec["aliases"]), now(), iid))
    return _settle(conn, iid, ref)


def note_sync(conn: sqlite3.Connection, ref: str, text: str, *,
              author: str = "user") -> dict[str, Any]:
    """A new note about the person of account `ref`, by the user or the agent."""
    text = _clean_text(text, NOTE_MAX, "The note")
    if not text:
        raise IdentityError("A note needs some text.")
    iid = _ensure(conn, ref)
    conn.execute("INSERT INTO person_notes (identity_id, text, author, created_at) "
                 "VALUES (?, ?, ?, ?)", (iid, text, author, now()))
    return _record(conn, iid)


def change_note_sync(conn: sqlite3.Connection, note_id: int, text: str | None, *,
                     author: str = "user") -> dict[str, Any]:
    """A note changed (`text`) or removed (None). The old row stays, marked replaced: what
    was once written about a person is still on record."""
    row = conn.execute("SELECT identity_id FROM person_notes WHERE id = ? AND "
                       "replaced_at IS NULL", (note_id,)).fetchone()
    if row is None:
        raise IdentityError("There is no such note.")
    iid = row[0]
    new = _clean_text(text or "", NOTE_MAX, "The note")
    at = now()
    conn.execute("UPDATE person_notes SET replaced_at = ? WHERE id = ?", (at, note_id))
    if new:
        conn.execute("INSERT INTO person_notes (identity_id, text, author, created_at) "
                     "VALUES (?, ?, ?, ?)", (iid, new, author, at))
    first = conn.execute("SELECT kb, person_id FROM identity_members WHERE identity_id = ? "
                         "LIMIT 1", (iid,)).fetchone()
    return _settle(conn, iid, person_ref(first[0], first[1]))
