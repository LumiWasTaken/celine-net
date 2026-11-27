"""A reference says what it names. A chat was `discord:18`, a person `discord:71` and a
message `discord:95095`: one shape for three things that each count from 1, so a
person's number also opened some other chat. Now a chat is `discord:chat:18`, a person
`discord:person:71` and a message `discord:msg:95095` (kb/identity.py).

Every stored old reference of a chat source is rewritten, in celine.db, log.db, the
notebook's notes and index, the user's prompt edits and the Routines. What an old
`source:N` named is read from where it stands:

- inside `[...]` or `[[...]]`: a message (a citation, a message line);
- after the word `chat` or `person`, in a `scope:` line, in `written_by=[...]`,
  `scope=[...]`, `chat=`, a `same person` line: that kind;
- in a tool call's arguments and its display, by the key it is under;
- a row of a count by person or by chat: that kind;
- anything else in the result of a Messages tool: a message;
- anything else in text someone wrote (a reply outside brackets, a note): left as it
  is, and counted in the log.

A source is a file `kb/<id>.db` with a `chats` table. `documents:812`, `web:3` and the
other one-kind references stay. Run again, it writes nothing: a new reference does not
match the old shape."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

CHAT_KEYS = {"scope", "chat", "chats", "scope_chats"}
PERSON_KEYS = {"written_by", "accounts", "members", "user_id", "from_user", "people"}
MSG_KEYS = {"ref", "refs", "message", "before", "after", "anchor", "first", "last",
            "earlier", "later", "hits", "missing", "invalid"}
# tables whose text is rewritten column by column with their own rules, or never
SKIP = {"messages", "tool_calls", "identities", "identity_members", "sqlite_sequence"}


def sources(db_path: Path) -> set[str]:
    out = set()
    for f in sorted((db_path.parent / "kb").glob("*.db")):
        try:
            c = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
            try:
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                             "name='chats'").fetchone():
                    out.add(f.stem)
            finally:
                c.close()
        except sqlite3.Error:
            continue
    return out


class Rewriter:
    """The rules above for one set of sources."""

    def __init__(self, kbs: set[str]):
        kb = "|".join(sorted(map(re.escape, kbs), key=len, reverse=True)) or r"(?!x)x"
        self.kbs = kbs
        self.old = re.compile(rf"(?<![\w:-])({kb}):(\d+)(?![\w:])")
        self.bracket = re.compile(rf"(\[\s*)({kb}):(\d+)(?![\w:])")
        self.worded = re.compile(rf"\b(chat|person)(\s+)({kb}):(\d+)(?![\w:])")
        self.arg = re.compile(r"\b(written_by|accounts|scope|chats|chat)\s*=\s*"
                              r"(\[[^\]\n]*\]|\"[^\"\n]*\"|'[^'\n]*')")
        self.left = 0

    def _all(self, text: str, kind: str) -> str:
        return self.old.sub(lambda m: f"{m[1]}:{kind}:{m[2]}", text)

    def cited(self, text: str) -> str:
        """Bracketed references only: what someone wrote keeps its words."""
        return self.bracket.sub(lambda m: f"{m[1]}{m[2]}:msg:{m[3]}", text)

    def prose(self, text: str) -> str:
        """Bracketed references, and the ones the words around them name."""
        text = self.cited(text)
        text = self.worded.sub(lambda m: f"{m[1]}{m[2]}{m[3]}:{m[1]}:{m[4]}", text)
        text = self.arg.sub(lambda m: m[1] + m[0][len(m[1]):len(m[0]) - len(m[2])] + self._all(
            m[2], "person" if m[1] in ("written_by", "accounts") else "chat"), text)
        self.left += len(self.old.findall(text))
        return text

    def result(self, text: str, by: str | None = None) -> str:
        """The result of a Messages tool, line by line."""
        out, people_follow = [], False
        row_kind = {"person": "person", "co_participant": "person", "chat": "chat"}.get(by or "")
        for line in text.split("\n"):
            s = line.strip()
            if s.startswith("scope:") or s.startswith("chat:"):
                line = self._all(line, "chat")
            elif s.startswith("person:") or "same person" in s or people_follow and \
                    line.startswith("  "):
                line = self._all(line, "person")
            elif row_kind and re.search(r"\([^()\n]*\): [\d,]+$", s):
                line = self._all(line, row_kind)
            people_follow = (people_follow and line.startswith("  ")) or \
                ("names" in s and "people. Pass" in s) or ("matches" in s and "people. Pass" in s)
            line = self.cited(line)
            line = self.worded.sub(lambda m: f"{m[1]}{m[2]}{m[3]}:{m[1]}:{m[4]}", line)
            line = re.sub(rf"({self.old.pattern})( is a (chat|person) reference)",
                          lambda m: f"{m[2]}:{m[5]}:{m[3]}{m[4]}", line)
            line = self.arg.sub(lambda m: m[1] + m[0][len(m[1]):len(m[0]) - len(m[2])]
                                + self._all(m[2], "person" if m[1] in ("written_by", "accounts")
                                            else "chat"), line)
            out.append(self._all(line, "msg"))
        return "\n".join(out)

    def walk(self, value, key: str | None = None, parent: str | None = None):
        """A JSON value with every string rewritten by the key it is under."""
        if isinstance(value, dict):
            return {k: self.walk(v, k, key) for k, v in value.items()}
        if isinstance(value, list):
            return [self.walk(v, key, parent) for v in value]
        if not isinstance(value, str):
            return value
        if key == "ref" and parent == "scope_chats" or key in CHAT_KEYS:
            return self._all(value, "chat")
        if key in PERSON_KEYS:
            return self._all(value, "person")
        if key in MSG_KEYS:
            return self._all(value, "msg")
        return self.prose(value)

    def json(self, raw: str | None) -> str | None:
        if not raw:
            return raw
        try:
            value = json.loads(raw)
        except ValueError:
            return self.prose(raw)
        new = self.walk(value)
        return raw if new == value else json.dumps(new, ensure_ascii=False)


def _has(conn, table: str, schema: str = "main") -> bool:
    return conn.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND "
                        f"name=?", (table,)).fetchone() is not None


def _like(rw: Rewriter, column: str) -> tuple[str, list[str]]:
    return (" OR ".join(f"{column} LIKE ?" for _ in rw.kbs) or "0",
            [f"%{k}:%" for k in sorted(rw.kbs)])


def _archive(name: str, rw: Rewriter) -> bool:
    return name.startswith("chat_archive__") or name.split("__")[0] in rw.kbs


def _messages(conn, rw: Rewriter) -> None:
    where, args = _like(rw, "m.content")
    rows = conn.execute(
        f"""SELECT m.rowid, m.role, m.content, t.name, t.arguments FROM messages m
            LEFT JOIN tool_calls t ON t.result_message_id = m.id WHERE {where}""",
        args).fetchall()
    for rowid, role, content, name, arguments in rows:
        if role == "tool" and name and _archive(name, rw):
            try:
                by = (json.loads(arguments or "{}") or {}).get("by")
            except (ValueError, AttributeError):
                by = None
            new = rw.result(content, by if name == "chat_archive__count" else None)
        elif role == "tool":
            new = rw.prose(content)
        else:
            new = rw.cited(content)
            rw.left += len(rw.old.findall(new))
        if new != content:
            conn.execute("UPDATE messages SET content = ? WHERE rowid = ?", (new, rowid))


def _tool_calls(conn, rw: Rewriter) -> None:
    for col in ("arguments", "display", "withheld_output"):
        where, args = _like(rw, col)
        for rowid, name, value, arguments in conn.execute(
                f"SELECT rowid, name, {col}, arguments FROM tool_calls WHERE {where}",
                args).fetchall():
            if col == "withheld_output":
                new = rw.result(value) if _archive(name, rw) else rw.prose(value)
            else:
                new = rw.json(value)
            if new != value:
                conn.execute(f"UPDATE tool_calls SET {col} = ? WHERE rowid = ?", (new, rowid))


def _other(conn, rw: Rewriter, schema: str = "main", skip: set[str] = SKIP) -> None:
    """Every other text column: text someone wrote, or JSON holding it."""
    tables = [r[0] for r in conn.execute(
        f"SELECT name, sql FROM {schema}.sqlite_master WHERE type='table'")
        if r[0] not in skip and "VIRTUAL" not in (r[1] or "").upper()
        and not re.search(r"_(fts|fts_data|fts_idx|fts_docsize|fts_config|fts_content)$", r[0])]
    for table in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")
                if (r[2] or "").upper() in ("TEXT", "")]
        for col in cols:
            where, args = _like(rw, f'"{col}"')
            try:
                rows = conn.execute(f'SELECT rowid, "{col}" FROM {schema}."{table}" '
                                    f"WHERE {where}", args).fetchall()
            except sqlite3.Error:            # a table without a rowid
                continue
            for rowid, value in rows:
                if not isinstance(value, str):
                    continue
                new = rw.json(value) if value[:1] in "[{" else rw.prose(value)
                if new != value:
                    conn.execute(f'UPDATE {schema}."{table}" SET "{col}" = ? WHERE rowid = ?',
                                 (new, rowid))


def _attached(conn, path: Path, rw: Rewriter, skip: set[str]) -> None:
    if not path.is_file():
        return
    conn.execute("ATTACH DATABASE ? AS other", (str(path),))
    try:
        conn.execute("BEGIN")
        _other(conn, rw, "other", skip)
        conn.execute("COMMIT")
    finally:
        conn.execute("DETACH DATABASE other")


def _files(db_path: Path, rw: Rewriter) -> None:
    root = db_path.parent
    # every note, its earlier versions (.versions) and the trashed ones (.trash): a
    # version restored later cites the way the rest does
    notes = [p for p in (root / "notebook").rglob("*.md") if p.is_file()]
    for path in [*notes, *(root / "prompts").glob("*.md"),
                 *(root / "settings" / "schedules").glob("*.toml"),
                 *(root / "settings" / "schedules").glob("*.md")]:
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if (new := rw.prose(text)) != text:
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(new)
            tmp.replace(path)


def up(conn, db_path) -> None:
    db_path = Path(db_path)
    rw = Rewriter(sources(db_path))
    if not rw.kbs:
        return
    conn.execute("BEGIN")
    _messages(conn, rw)
    _tool_calls(conn, rw)
    _other(conn, rw)
    conn.execute("COMMIT")
    # requests and raw replies are a diagnostic record of what went over the wire
    _attached(conn, db_path.parent / "log.db", rw, {"requests", "raw_responses"})
    _attached(conn, db_path.parent / "notebook" / ".index.db", rw, set())
    _files(db_path, rw)
    if rw.left:
        log.info("old references left in written text",
                 extra={"event": "db.typed_refs_left", "count": rw.left})
