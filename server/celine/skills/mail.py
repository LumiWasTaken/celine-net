"""Email over IMAP (guide guides/email.md): its own inbox, it manages it. The account is
set in Settings, Email (settings/mail.py).

One connection per call, in a worker thread, with a timeout. Listing, searching and
attachments open folders with EXAMINE and fetch with BODY.PEEK; reading a message marks it
read, and mark, move and delete change what it asks. A message id is `<folder>:<uid>`.
Every failure is one sentence (MailError).
"""
from __future__ import annotations

import base64
import email
import email.policy
import imaplib
import re
import socket
import ssl
import tempfile
import time
from datetime import date, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Annotated, Any, Callable

import anyio

from ..settings.mail import Account, account
from ..triggers import Event
from .. import vision
from . import ToolContext, ToolResult, guide, tool, described

TIMEOUT = 30            # seconds: the connection and each command of one call
FRAGMENT_TIMEOUT = 5    # the unread count in its prompt must not hold up a turn
UNREAD_TTL = 60         # seconds the unread count is kept
LIST_DEFAULT, LIST_MAX = 20, 100
READ_TOKENS = 6000      # one part of a long body or attachment
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
           "Dec")
_HEADERS = "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE CONTENT-TYPE)]"


class MailError(Exception):
    """One plain sentence, for it or for Settings."""


# ---- IMAP --------------------------------------------------------------------------------

def _decode_folder(s: str) -> str:
    """IMAP's modified UTF-7 folder names (RFC 3501 5.1.3) to text."""
    def dec(m: re.Match) -> str:
        b = m.group(1).replace(",", "/")
        if not b:
            return "&"
        return base64.b64decode(b + "=" * (-len(b) % 4)).decode("utf-16-be", "replace")
    return re.sub(r"&([A-Za-z0-9+,]*)-", dec, s)


def _encode_folder(s: str) -> str:
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            b = base64.b64encode("".join(run).encode("utf-16-be")).decode()
            out.append("&" + b.rstrip("=").replace("/", ",") + "-")
            run.clear()
    for ch in s:
        if " " <= ch <= "~":
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            run.append(ch)
    flush()
    return "".join(out)


def _q(s: str) -> str:
    """A quoted IMAP string; a line break would end the command, so it is a space."""
    s = s.replace("\r", " ").replace("\n", " ")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _text(data: Any) -> str:
    return " ".join(b" ".join(d for d in data if isinstance(d, bytes))
                    .decode("utf-8", "replace").split())[:200]


def _ok(res: tuple, what: str) -> list:
    typ, data = res
    if typ != "OK":
        raise MailError(f"The mail server refused {what}: {_text(data) or typ}.")
    return data


def _open(a: Account, timeout: float) -> imaplib.IMAP4:
    tls = ssl.create_default_context()
    if a.tls == "implicit":
        return imaplib.IMAP4_SSL(a.host, a.port, ssl_context=tls, timeout=timeout)
    m = imaplib.IMAP4(a.host, a.port, timeout=timeout)
    m.starttls(tls)
    return m


def session(a: Account, fn: Callable[[imaplib.IMAP4], Any], timeout: float = TIMEOUT):
    """Connect, log in, run fn(imap), log out. Blocking: call it in a worker thread."""
    where = f"{a.host}:{a.port}"
    try:
        m = _open(a, timeout)
    except socket.gaierror:
        raise MailError(f"The app cannot find the mail server {a.host}.") from None
    except ConnectionRefusedError:
        raise MailError(f"The mail server at {where} refused the connection.") from None
    except TimeoutError:
        raise MailError(f"The mail server at {where} did not answer within "
                        f"{timeout:.0f} seconds.") from None
    except ssl.SSLError:
        raise MailError(f"The secure connection to {where} failed: check the port and "
                        f"the encryption.") from None
    except imaplib.IMAP4.error as e:
        raise MailError(f"The mail server at {where} answered: {e}.") from None
    except OSError as e:
        raise MailError(f"The app could not reach the mail server at {where}: "
                        f"{e.strerror or e}.") from None
    try:
        try:
            if (a.user + a.password).isascii():
                m.login(a.user, a.password)
            else:           # LOGIN is ASCII only; PLAIN carries UTF-8
                m.authenticate("PLAIN", lambda _: f"\0{a.user}\0{a.password}".encode())
        except imaplib.IMAP4.error:
            raise MailError(f"The mail server at {a.host} refused the login for "
                            f"{a.user}: check the password in Settings, Email.") from None
        return fn(m)
    except TimeoutError:
        raise MailError(f"The mail server at {where} stopped answering.") from None
    except imaplib.IMAP4.error as e:
        raise MailError(f"The mail server at {where} answered: {e}.") from None
    except OSError as e:
        raise MailError(f"The connection to {where} broke: {e.strerror or e}.") from None
    finally:
        try:
            m.logout()
        except Exception:                       # noqa: BLE001 - leaving anyway
            pass


_LIST = re.compile(rb'^\((?P<flags>[^)]*)\) (?:"(?:[^"\\]|\\.)*"|NIL) (?P<name>.*)$')


def _folder_names(m: imaplib.IMAP4) -> list[str]:
    """Every folder that can be opened."""
    return [name for name, _ in _folder_flags(m)]


def _folder_flags(m: imaplib.IMAP4) -> list[tuple[str, bytes]]:
    """Every folder that can be opened, with its LIST flags in lowercase."""
    out = []
    for item in _ok(m.list(), "the folder list"):
        meta, raw = (item[0], item[1]) if isinstance(item, tuple) else (item, None)
        hit = _LIST.match(meta or b"")
        if hit is None:
            continue
        if raw is None:
            raw = hit.group("name")
            if raw.startswith(b'"'):
                raw = re.sub(rb"\\(.)", rb"\1", raw[1:-1])
        flags = hit.group("flags").lower()
        if b"\\noselect" in flags or b"\\nonexistent" in flags:
            continue
        out.append((_decode_folder(raw.decode("utf-8", "replace")), flags))
    return out


_TRASH_NAMES = ("trash", "deleted items", "deleted messages", "papierkorb", "gelöscht")


def _trash(m: imaplib.IMAP4) -> str | None:
    """The trash folder: the one the server flags \\Trash, else one named like it."""
    found = _folder_flags(m)
    for name, flags in found:
        if b"\\trash" in flags:
            return name
    return next((n for n, _ in found if n.rpartition("/")[2].lower() in _TRASH_NAMES), None)


def _status(m: imaplib.IMAP4, folder: str, items: str = "MESSAGES UNSEEN") -> dict[str, int]:
    data = _ok(m.status(_q(_encode_folder(folder)), f"({items})"), f"the counts of {folder}")
    line = data[-1] if isinstance(data[-1], bytes) else b""
    return {k.decode().lower(): int(v)
            for k, v in re.findall(rb"([A-Z]+) (\d+)", line.rsplit(b"(", 1)[-1])}


def folders(a: Account) -> list[dict[str, Any]]:
    """Every folder with its message and unread counts. Blocking."""
    def run(m):
        out = []
        for name in _folder_names(m):
            try:
                st = _status(m, name)
            except MailError:
                st = {}
            out.append({"name": name, "messages": st.get("messages"),
                        "unread": st.get("unseen")})
        return out
    return session(a, run)


def _examine(m: imaplib.IMAP4, folder: str) -> None:
    """Open a folder read-only."""
    typ, _ = m.select(_q(_encode_folder(folder)), readonly=True)
    if typ != "OK":
        raise MailError(f"There is no folder {folder!r}. email__list_folders lists them.")


def _select(m: imaplib.IMAP4, folder: str) -> None:
    """Open a folder to change it."""
    typ, _ = m.select(_q(_encode_folder(folder)))
    if typ != "OK":
        raise MailError(f"There is no folder {folder!r}. email__list_folders lists them.")


def _by_folder(ids: list[str]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for mid in ids:
        folder, uid = _split_id(mid)
        out.setdefault(folder, []).append(uid)
    return out


def _present(m: imaplib.IMAP4, uids: list[int]) -> list[int]:
    """Of these uids in the open folder, the ones still there."""
    return _uids(m, ["UID", ",".join(map(str, uids))]) if uids else []


def _expunge(m: imaplib.IMAP4, uids: list[int]) -> None:
    """Delete these messages of the open folder for good."""
    s = ",".join(map(str, uids))
    _ok(m.uid("STORE", s, "+FLAGS.SILENT", r"(\Deleted)"), "the delete")
    if "UIDPLUS" in m.capabilities:
        _ok(m.uid("EXPUNGE", s), "the delete")
    else:
        # note: plain EXPUNGE also removes other messages already marked deleted in
        # this folder; servers without UIDPLUS are rare
        _ok(m.expunge(), "the delete")


def _move(m: imaplib.IMAP4, uids: list[int], to: str) -> None:
    """Move these messages of the open folder to another folder."""
    s, dest = ",".join(map(str, uids)), _q(_encode_folder(to))
    if "MOVE" in m.capabilities:
        _ok(m.uid("MOVE", s, dest), f"the move to {to}")
    else:
        _ok(m.uid("COPY", s, dest), f"the copy to {to}")
        _expunge(m, uids)


def _uids(m: imaplib.IMAP4, criteria: list[str], literal: bytes | None = None) -> list[int]:
    if literal is not None:
        m.literal = literal     # sent after the last criterion, as its value
    data = _ok(m.uid("SEARCH", *criteria), "the search")
    return sorted(int(x) for d in data if isinstance(d, bytes) for x in d.split())


def _fetch(m: imaplib.IMAP4, uids: list[int], what: str) -> list[tuple[bytes, bytes]]:
    """(meta, literal) per message. Meta carries UID, FLAGS and RFC822.SIZE, whether the
    server sends them before or after the literal."""
    data = _ok(m.uid("FETCH", ",".join(map(str, uids)), what), "the fetch")
    out: list[list[bytes]] = []
    for item in data:
        if isinstance(item, tuple):
            out.append([item[0], item[1]])
        elif isinstance(item, bytes) and out:
            out[-1][0] += item
    return [(a, b) for a, b in out]


def _meta(meta: bytes) -> dict[str, Any]:
    uid = re.search(rb"UID (\d+)", meta)
    size = re.search(rb"RFC822\.SIZE (\d+)", meta)
    flags = re.search(rb"FLAGS \(([^)]*)\)", meta)
    return {"uid": int(uid.group(1)) if uid else None,
            "size": int(size.group(1)) if size else None,
            "unread": flags is not None and b"\\seen" not in flags.group(1).lower()}


def _criteria(query: str | None, from_: str | None, since: str | None
              ) -> tuple[list[str], bytes | None]:
    """IMAP SEARCH criteria and the one value sent as a UTF-8 literal (the last)."""
    crit: list[str] = []
    if since:
        try:
            d = date.fromisoformat(since.strip())
        except ValueError:
            raise MailError(f"since takes a date like 2026-01-01, not {since!r}.") from None
        crit += ["SINCE", f"{d.day}-{_MONTHS[d.month - 1]}-{d.year}"]
    wide: tuple[str, str] | None = None
    for key, v in (("FROM", from_), ("TEXT", query)):
        v = (v or "").strip()
        if not v:
            continue
        if v.isascii():
            crit += [key, _q(v)]
        elif wide is None:
            wide = (key, v)
        else:
            raise MailError("The mail server takes one value with letters beyond ASCII per "
                            "search: search by the words or by the sender.")
    if wide is None:
        return crit or ["ALL"], None
    return ["CHARSET", "UTF-8", *crit, wide[0]], wide[1].encode()


# ---- messages ----------------------------------------------------------------------------

def _parse(raw: bytes) -> email.message.EmailMessage:
    return email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[return-value]


def _h(msg, name: str) -> str:
    try:
        return " ".join(str(msg.get(name) or "").split())
    except Exception:                           # noqa: BLE001 - a malformed header
        return ""


def _date(msg, tz) -> str:
    try:
        d = parsedate_to_datetime(msg.get("date"))
    except Exception:                           # noqa: BLE001 - missing or malformed
        return "no date"
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return f"{d.astimezone(tz):%Y-%m-%d %H:%M}"


def _size(n: int | None) -> str:
    if n is None:
        return "? bytes"
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _body(msg) -> str:
    """The text of a message: its plain part, else its HTML part as text."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:                           # noqa: BLE001 - an unknown charset
        text = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        from ..kb.documents_extract import html_text
        text = "\n\n".join(s.text for s in html_text(text).segments)
    return text.strip()


def _files(msg) -> list:
    """Every part with a file name: attachments and named inline parts."""
    return [p for p in msg.walk() if not p.is_multipart() and p.get_filename()]


def _file_line(p) -> str:
    return (f"{p.get_filename()} ({p.get_content_type()}, "
            f"{_size(len(p.get_payload(decode=True) or b''))})")


def _split_id(mid: str) -> tuple[str, int]:
    folder, _, uid = str(mid).strip().rpartition(":")
    if not folder or not uid.isdigit():
        raise MailError(f"{mid!r} is not a message id like INBOX:4521.")
    return folder, int(uid)


def _paged(text: str, part: int | None, budget: int) -> tuple[str, int, int]:
    from .web import split_parts
    parts = split_parts(text, budget) if text else [""]
    k = max(1, min(int(part or 1), len(parts)))
    return parts[k - 1], k, len(parts)


# ---- the skill ---------------------------------------------------------------------------

_unread: dict[tuple, tuple[float, int | None]] = {}     # account -> (when, unread)


class Email:
    name = "email"
    guide = guide("email")
    description = described()
    error = "{{username}} has not set up the email account yet (Settings, Email)."

    app = None
    triggers = (Event(
        "mail.new", source="new_mail", every=300, label="New email",
        about="Reads new mail and asks you only when something needs you.",
        outside=("from", "subject"),
        prompt="New mail in the inbox: {id}, from {from}, \"{subject}\", {date}. Read it "
               "with email__read if the subject is not enough to judge it."),)

    def __init__(self, cfg):
        self.cfg = cfg

    async def new_mail(self, store) -> list[dict[str, str]]:
        """The mail.new source: the unread inbox messages since the last ask, one dict
        each (id, from, subject, date). The first ask only notes where the inbox is."""
        a = account(self.cfg)
        if a is None:
            return []
        key = "trigger:mail.new:last_uid"
        last = await store.kv_get(key)
        tz = self._tz()

        def run(m):
            _examine(m, "INBOX")
            if last is None:
                return (_uids(m, ["ALL"]) or [0])[-1], []
            new = [u for u in _uids(m, ["UID", f"{int(last) + 1}:*", "UNSEEN"])
                   if u > int(last)]        # n:* always matches the newest one
            rows = _fetch(m, new, f"(UID FLAGS {_HEADERS})") if new else []
            return max(new, default=int(last)), rows
        top, rows = await anyio.to_thread.run_sync(lambda: session(a, run))
        await store.kv_set(key, str(top))
        out = []
        for meta, head in rows:
            msg = _parse(head)
            out.append({"id": f"INBOX:{_meta(meta)['uid']}",
                        "from": _h(msg, "from") or "(unknown)",
                        "subject": _h(msg, "subject") or "(no subject)",
                        "date": _date(msg, tz)})
        return out

    @property
    def status(self) -> str:
        return "up" if account(self.cfg) else "down"

    def _tz(self):
        from ..clock import zone
        return zone(self.cfg)

    def _budget(self, ctx: ToolContext) -> int:
        if ctx.max_output_tokens:
            return max(800, min(READ_TOKENS, int(ctx.max_output_tokens) - 300))
        return READ_TOKENS

    async def _run(self, fn: Callable[[imaplib.IMAP4], Any]) -> Any:
        a = account(self.cfg)
        if a is None:
            raise MailError(self.cfg.substitute(self.error))
        return await anyio.to_thread.run_sync(lambda: session(a, fn))

    async def prompt_fragment(self, ctx: ToolContext) -> str | None:
        a = account(self.cfg)
        if a is None:
            return None
        key = (a.host, a.port, a.user)
        hit = _unread.get(key)
        if hit is None or time.monotonic() - hit[0] > UNREAD_TTL:
            try:
                n = await anyio.to_thread.run_sync(lambda: session(
                    a, lambda m: _status(m, "INBOX", "UNSEEN").get("unseen"),
                    FRAGMENT_TIMEOUT))
            except MailError:
                n = None
            hit = _unread[key] = (time.monotonic(), n)
        if hit[1] is None:
            return f"Email: {a.user} on {a.host}, the mail server did not answer just now."
        return f"Email: {a.user} on {a.host}, {hit[1]} unread."

    def _lines(self, folder: str, rows: list[tuple[bytes, bytes]]) -> list[str]:
        tz = self._tz()
        out = []
        for info, head in sorted(((_meta(meta), head) for meta, head in rows),
                                 key=lambda r: -(r[0]["uid"] or 0)):
            msg = _parse(head)
            notes = [_size(info["size"])]
            # note: multipart/mixed stands for "has attachments"; BODYSTRUCTURE
            # would be exact
            if msg.get_content_type() == "multipart/mixed":
                notes.append("attachments")
            if info["unread"]:
                notes.append("unread")
            subject = _h(msg, "subject") or "(no subject)"
            out.append(f"[{folder}:{info['uid']}] {_date(msg, tz)}  from "
                       f"{_h(msg, 'from') or '(unknown)'}  \"{subject}\"  "
                       f"({', '.join(notes)})")
        return out

    @tool(open_world=True)
    async def list(self, ctx: ToolContext,
                   folder: Annotated[str, "A folder name from email__list_folders."] = "INBOX",
                   unread_only: Annotated[bool, "Only unread messages."] = False,
                   limit: Annotated[int | None, "The most messages, default 20, at most "
                                                "100."] = None,
                   before: Annotated[str | None, "A message id: list the messages older than "
                                                 "it. The `more:` line gives it."] = None
                   ) -> ToolResult:
        """List the messages of one folder, newest first. Use email__search to find
        messages by words, sender or date. Returns: one line per message with its id,
        date, sender, subject, size, attachments and unread, then a `more:` line."""
        n = max(1, min(int(limit or LIST_DEFAULT), LIST_MAX))
        try:
            older = int(before.rpartition(":")[2]) if before else None
        except ValueError:
            return ToolResult(content=f"before takes a message id like {folder}:4521.",
                              is_error=True)

        def run(m):
            _examine(m, folder)
            uids = _uids(m, ["UNSEEN" if unread_only else "ALL"])
            total = len(uids)
            if older is not None:
                uids = [u for u in uids if u < older]
            page = uids[-n:]
            rows = _fetch(m, page, f"(UID FLAGS RFC822.SIZE {_HEADERS})") if page else []
            return total, rows, page[0] if len(uids) > n else None
        try:
            total, rows, more = await self._run(run)
        except MailError as e:
            return ToolResult(content=str(e), is_error=True)
        what = "unread messages" if unread_only else "messages"
        if not rows:
            return ToolResult(content=f"{folder}: no {what}"
                                      + (" older than that." if older else "."),
                              display={"folder": folder, "messages": 0})
        lines = [f"{folder}: {total} {what}, newest first:", *self._lines(folder, rows)]
        if more:
            lines.append(f"more: email__list(folder=\"{folder}\", before=\"{folder}:{more}\""
                         + (", unread_only=true" if unread_only else "") + ")")
        return ToolResult(content="\n".join(lines),
                          display={"folder": folder, "messages": len(rows), "total": total})

    @tool(open_world=True)
    async def search(self, ctx: ToolContext,
                     query: Annotated[str | None, "Words in the subject, the headers or the "
                                                  "text."] = None,
                     folder: Annotated[str, "A folder name from email__list_folders."] = "INBOX",
                     since: Annotated[str | None, "Only messages from this day on, such as "
                                                  "2026-01-01."] = None,
                     from_: Annotated[str | None, "Words in the sender: a name or an "
                                                  "address."] = None,
                     limit: Annotated[int | None, "The most messages, default 20, at most "
                                                  "100."] = None) -> ToolResult:
        """Search one folder with the mail server's own search, by words, sender and date.
        Use email__list to see the newest messages without a filter. Returns: the count,
        then one line per match, newest first."""
        n = max(1, min(int(limit or LIST_DEFAULT), LIST_MAX))
        try:
            crit, literal = _criteria(query, from_, since)
        except MailError as e:
            return ToolResult(content=str(e), is_error=True)

        def run(m):
            _examine(m, folder)
            uids = _uids(m, crit, literal)
            page = uids[-n:]
            rows = _fetch(m, page, f"(UID FLAGS RFC822.SIZE {_HEADERS})") if page else []
            return len(uids), rows
        try:
            total, rows = await self._run(run)
        except MailError as e:
            return ToolResult(content=str(e), is_error=True)
        if not rows:
            return ToolResult(content=f"{folder}: no message matches.",
                              display={"folder": folder, "matches": 0})
        head = f"{folder}: {total} match{'es' if total != 1 else ''}" + \
            (f", the newest {len(rows)}:" if total > len(rows) else ", newest first:")
        return ToolResult(content="\n".join([head, *self._lines(folder, rows)]),
                          display={"folder": folder, "matches": total})

    async def _message(self, mid: str, mark_read: bool = False):
        """One message; mark_read opens it the way a mail client does, marking it read."""
        folder, uid = _split_id(mid)

        def run(m):
            (_select if mark_read else _examine)(m, folder)
            rows = _fetch(m, [uid], "(UID FLAGS BODY.PEEK[])")
            if not rows:
                raise MailError(f"There is no message {mid} anymore.")
            if mark_read and _meta(rows[0][0])["unread"]:
                # peek, then mark: the flags it sees are the ones before it opened it
                _ok(m.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Seen)"), "marking it read")
            return rows[0]
        meta, raw = await self._run(run)
        return folder, uid, _meta(meta), _parse(raw)

    @tool(read_only=False, destructive=False, open_world=True)     # reading marks it \Seen
    async def read(self, ctx: ToolContext,
                   id: Annotated[str, "A message id from email__list or email__search, "
                                      "such as INBOX:4521."],
                   part: Annotated[int | None, "Which part of a long message, from 1."] = None
                   ) -> ToolResult:
        """Read one message: its headers, its attachments by name, type and size, and its
        text. HTML comes as plain text. Reading marks it read; email__mark sets it unread
        again. Returns: the headers, then one part of the text with an `earlier:` and
        `later:` line."""
        try:
            folder, uid, info, msg = await self._message(id, mark_read=True)
        except MailError as e:
            return ToolResult(content=str(e), is_error=True)
        if info["unread"]:
            _unread.clear()
        files = _files(msg)
        lines = [f"id: {folder}:{uid}" + (" (was unread)" if info["unread"] else "")]
        for name in ("From", "To", "Cc", "Reply-To"):
            if v := _h(msg, name):
                lines.append(f"{name}: {v}")
        lines += [f"Date: {_date(msg, self._tz())}",
                  f"Subject: {_h(msg, 'subject') or '(no subject)'}"]
        if files:
            lines.append("Attachments: " + "; ".join(_file_line(p) for p in files))
        text, k, total = _paged(_body(msg), part, self._budget(ctx))
        if total > 1:
            lines.append(f"(part {k} of {total})")
        lines += ["", text or "(no text)"]
        if total > 1:
            lines.append("")
            if k > 1:
                lines.append(f"earlier: email__read(id=\"{folder}:{uid}\", part={k - 1})")
            lines.append(f"later: email__read(id=\"{folder}:{uid}\", part={k + 1})"
                         if k < total else "end of the message")
        return ToolResult(content="\n".join(lines),
                          display={"id": f"{folder}:{uid}", "part": k, "parts": total,
                                   "attachments": len(files)})

    async def _change(self, ids: list[str], each: Callable[[imaplib.IMAP4, str, list[int]], None]
                      ) -> tuple[list[str], list[str]]:
        """Run each(imap, folder, uids) on the messages still there, folder by folder.
        Returns (the ids changed, the ids not there anymore)."""
        groups = _by_folder(ids)

        def run(m):
            done, gone = [], []
            for folder, uids in groups.items():
                _select(m, folder)
                here = _present(m, uids)
                if here:
                    each(m, folder, here)
                done += [f"{folder}:{u}" for u in here]
                gone += [f"{folder}:{u}" for u in uids if u not in here]
            return done, gone
        done, gone = await self._run(run)
        _unread.clear()
        return done, gone

    @staticmethod
    def _said(what: str, done: list[str], gone: list[str]) -> ToolResult:
        """"2 messages marked read: INBOX:3, INBOX:9." and the ids that were gone."""
        n = len(done)
        line = f"{n} message{'' if n == 1 else 's'} {what}" + (f": {', '.join(done)}." if done
                                                               else ".")
        if gone:
            line += f" Not there anymore: {', '.join(gone)}."
        return ToolResult(content=line, is_error=not done,
                          display={"messages": n, "missing": len(gone)})

    @tool(read_only=False, open_world=True)
    async def mark(self, ctx: ToolContext,
                   ids: Annotated[list[str], "Message ids such as INBOX:4521."],
                   state: Annotated[str, "read, unread, flagged or unflagged."]) -> ToolResult:
        """Mark messages read or unread, or flag and unflag them. Reading a message
        already marks it read. Returns: what changed."""
        flag = {"read": ("+", "\\Seen"), "unread": ("-", "\\Seen"),
                "flagged": ("+", "\\Flagged"), "unflagged": ("-", "\\Flagged")}.get(
            state.strip().lower())
        if flag is None:
            return ToolResult(content="state is read, unread, flagged or unflagged.",
                              is_error=True)

        def each(m, folder, uids):
            _ok(m.uid("STORE", ",".join(map(str, uids)), f"{flag[0]}FLAGS.SILENT",
                      f"({flag[1]})"), f"marking in {folder}")
        try:
            done, gone = await self._change(ids, each)
        except MailError as e:
            return ToolResult(content=str(e), is_error=True)
        return self._said(f"marked {state.strip().lower()}", done, gone)

    @tool(read_only=False, open_world=True)
    async def move(self, ctx: ToolContext,
                   ids: Annotated[list[str], "Message ids such as INBOX:4521."],
                   folder: Annotated[str, "The folder to move them to. A folder that does "
                                          "not exist yet is made."]) -> ToolResult:
        """Move messages to another folder, making the folder when it is new. The moved
        messages get new ids there: email__list shows them. Returns: what moved."""
        to = folder.strip()
        if not to:
            return ToolResult(content="folder names where the messages go.", is_error=True)
        made: list[str] = []

        def each(m, src, uids):
            if not made and to not in _folder_names(m):
                _ok(m.create(_q(_encode_folder(to))), f"making the folder {to}")
                made.append(to)
            _move(m, uids, to)
        try:
            done, gone = await self._change(ids, each)
        except MailError as e:
            return ToolResult(content=str(e), is_error=True)
        out = self._said(f"moved to {to}", done, gone)
        if made:
            out.content += f" The folder {to} is new."
        return out

    @tool(read_only=False, open_world=True)
    async def delete(self, ctx: ToolContext,
                     ids: Annotated[list[str], "Message ids such as INBOX:4521."]) -> ToolResult:
        """Delete messages: they move to the trash folder. Returns: what moved."""
        where: list[str] = []
        kept: list[str] = []

        def each(m, folder, uids):
            if not where:
                where.append(_trash(m) or "")
            trash = where[0]
            if not trash:
                raise MailError("This account has no trash folder.")
            if folder == trash:
                kept.extend(f"{folder}:{u}" for u in uids)
            else:
                _move(m, uids, trash)
        try:
            done, gone = await self._change(ids, each)
        except MailError as e:
            return ToolResult(content=str(e), is_error=True)
        moved = [d for d in done if d not in kept]
        out = self._said(f"moved to {where[0]}", moved, gone)
        if kept:
            out.content += f" Already in {where[0]}: {', '.join(kept)}."
        return out

    @tool(open_world=True)
    async def list_folders(self, ctx: ToolContext) -> ToolResult:
        """List the folders of the account with their message and unread counts. Returns:
        one line per folder."""
        a = account(self.cfg)
        if a is None:
            return ToolResult(content=self.cfg.substitute(self.error), is_error=True)
        try:
            found = await anyio.to_thread.run_sync(lambda: folders(a))
        except MailError as e:
            return ToolResult(content=str(e), is_error=True)
        lines = [f"{f['name']}: {f['messages'] if f['messages'] is not None else '?'} "
                 f"messages, {f['unread'] if f['unread'] is not None else '?'} unread"
                 for f in found]
        return ToolResult(content="\n".join(lines) or "No folders.",
                          display={"folders": len(found)})

    @tool(open_world=True)
    async def get_attachment(self, ctx: ToolContext,
                             id: Annotated[str, "The message id, such as INBOX:4521."],
                             name: Annotated[str, "The attachment's file name, as "
                                                  "email__read lists it."],
                             part: Annotated[int | None, "Which part of a long text, "
                                                         "from 1."] = None) -> ToolResult:
        """Get one attachment of a message. It is attached to this conversation, so
        {{username}} can open it. A text, PDF, Word or HTML file comes back as text; an
        image comes back described. Returns: the text or the description."""
        try:
            folder, uid, _, msg = await self._message(id)
        except MailError as e:
            return ToolResult(content=str(e), is_error=True)
        files = _files(msg)
        p = next((f for f in files if f.get_filename() == name), None) or \
            next((f for f in files if f.get_filename().lower() == name.strip().lower()), None)
        if p is None:
            have = ", ".join(f.get_filename() for f in files) or "none"
            return ToolResult(content=f"{folder}:{uid} has no attachment {name!r}. Its "
                                      f"attachments: {have}.", is_error=True)
        data = p.get_payload(decode=True) or b""
        filename = Path(p.get_filename()).name or "attachment"
        display: dict[str, Any] = {"id": f"{folder}:{uid}", "name": filename}
        if not part or part == 1:      # attached once, not again for each later part
            from .. import attachments
            row = await attachments.save_generated(self.cfg, ctx.store, data, filename,
                                                   p.get_content_type(), ctx.tool_call_id)
            display["attachment"] = {k: row[k] for k in ("id", "sha256", "filename",
                                                         "media_type", "bytes")}
        head = f"{filename} ({p.get_content_type()}, {_size(len(data))}) from {folder}:{uid}" \
               + (f", attached to this conversation for {self.cfg.user}." if "attachment"
                  in display else ".")
        from ..kb.documents_extract import extract, limits

        def read_it():
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / filename
                path.write_bytes(data)
                return extract(path, limits(self.cfg))
        got = await anyio.to_thread.run_sync(read_it)
        if got.kind == "image":
            llm = self.app.llm if self.app is not None else None
            said = await vision.describe(llm, data, ctx.conversation_id, ctx.turn_id)
            return ToolResult(content=f"{head}\n\n{said}", display=display)
        text = "\n\n".join(s.text for s in got.segments).strip()
        if not text:
            why = got.error or "the app cannot read text out of this kind of file"
            return ToolResult(content=f"{head}\n\nNo text: {why}.", display=display)
        body, k, total = _paged(text, part, self._budget(ctx))
        lines = [head + (f" (part {k} of {total})" if total > 1 else ""), "", body]
        if total > 1:
            lines.append("")
            ref = f"id=\"{folder}:{uid}\", name={p.get_filename()!r}"
            if k > 1:
                lines.append(f"earlier: email__get_attachment({ref}, part={k - 1})")
            lines.append(f"later: email__get_attachment({ref}, part={k + 1})"
                         if k < total else "end of the attachment")
        return ToolResult(content="\n".join(lines), display={**display, "part": k,
                                                             "parts": total})


async def probe(cfg, services) -> dict[str, str]:
    """Settings, Features: an account is saved (Settings, Email). Nothing is connected
    here; the mail skill says when the server refuses."""
    a = account(cfg)
    if a is None:
        return {"state": "unset", "detail": "Fill in the account below."}
    return {"state": "ready", "detail": f"{a.user} on {a.host}."}
