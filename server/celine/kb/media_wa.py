"""WhatsApp media files: where they come from.

1. `collect_meta`: what WhatsApp says about each media message's file, into
   `wa_media_meta`: the plain file's sha256 (msgstore.db `message_media.file_hash`,
   the linked device's `ref.file_sha256`), the file name/path on the phone, size, mime,
   and the CDN reference (direct path, media key, encrypted hash) for a later download.
   Reads a decrypted msgstore.db and the `ref` wa-link puts in each archived media
   message; read only.
2. `scan_folder`: the phone's `WhatsApp/Media` folder, copied to this machine once.
   Every file is hashed and matched to its messages: by content hash (exact), else by
   the file name WhatsApp recorded (the phone's name in msgstore.db or from the linked
   device, or the name a .txt export wrote in the message, e.g.
   `IMG-20230105-WA0007.jpg (file attached)`; same size, or "resaved": same pixel size
   and day, because gallery/sync apps rewrite files), else (only when asked) by the
   WhatsApp name pattern `IMG-20230105-WA0007.jpg` when exactly one message of that
   type on that day has no file and no recorded name. A dry run reports; a real run
   copies each matched file to `<kb>.media/<sha256>` and adds it to the message's
   `media.files`.
3. `fetch_via_link`: wa-link downloads from WhatsApp's CDN by reference
   (wa-link `/admin/media/download`); a file whose CDN copy expired can be asked
   from the user's OWN phone (`/admin/media/retry`, a media retry receipt to the own account). Flags `[media] wa_download`,
   `wa_media_retry`; the retry also needs `retry = true` on the call, so it never happens as a side effect.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import re
import shutil
import struct
import time
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .importers import ImportContext, Unreachable
from ..core.ids import now_ms
from .documents import sha256_file
from .media import media_dir

# msgstore.db message_type -> media type
MEDIA_TYPES = {1: "photo", 2: "audio", 3: "video", 4: "contact", 5: "location",
               9: "document", 13: "gif", 14: "contact", 16: "location", 20: "sticker",
               42: "photo", 43: "video", 82: "file"}

# IMG-20230105-WA0007.jpg, VID-..., PTT-..., AUD-..., STK-..., DOC-...
_WA_NAME = re.compile(r"^(IMG|VID|PTT|AUD|STK|DOC)-(\d{8})-WA\d+", re.I)
_NAME_TYPES = {"IMG": {"photo"}, "VID": {"video", "gif"}, "PTT": {"voice", "audio"},
               "AUD": {"audio", "voice"}, "STK": {"sticker"}, "DOC": {"document", "file"}}
_SKIP_NAMES = {".nomedia", "thumbs.db", ".ds_store"}


def hex_hash(v: Any) -> str | None:
    """WhatsApp stores sha256 as base64 (msgstore text, protojson bytes); we use hex."""
    if v is None or v == "":
        return None
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex() if len(v) == 32 else None
    s = str(v).strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", s):
        return s.lower()
    try:
        raw = base64.b64decode(s + "=" * (-len(s) % 4), altchars=b"-_" if "-" in s or "_" in s else None)
    except (binascii.Error, ValueError):
        return None
    return raw.hex() if len(raw) == 32 else None


def _b64(v: Any) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, (bytes, bytearray)):
        return base64.b64encode(bytes(v)).decode()
    return str(v)


def _int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ---- 1. metadata ---------------------------------------------------------------------------

_UPSERT = """INSERT INTO wa_media_meta (key, source, mtype, file_hash, file_name, file_path,
                 file_size, mime, direct_path, media_key, enc_file_hash, media_key_ts, ts,
                 width, height)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
             ON CONFLICT (key, source) DO UPDATE SET
               mtype = excluded.mtype, file_hash = COALESCE(excluded.file_hash, file_hash),
               file_name = COALESCE(excluded.file_name, file_name),
               file_path = COALESCE(excluded.file_path, file_path),
               file_size = COALESCE(excluded.file_size, file_size),
               mime = COALESCE(excluded.mime, mime),
               direct_path = COALESCE(excluded.direct_path, direct_path),
               media_key = COALESCE(excluded.media_key, media_key),
               enc_file_hash = COALESCE(excluded.enc_file_hash, enc_file_hash),
               media_key_ts = COALESCE(excluded.media_key_ts, media_key_ts),
               ts = COALESCE(excluded.ts, ts),
               width = COALESCE(excluded.width, width),
               height = COALESCE(excluded.height, height)"""


def msgstore_rows(path: Path) -> Iterator[tuple]:
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(message_media)")}
        if not cols:
            return
        want = ["file_hash", "file_path", "file_size", "mime_type", "media_name",
                "direct_path", "media_key", "enc_file_hash", "media_key_timestamp",
                "width", "height"]
        sel = ", ".join(f"mm.{c}" if c in cols else f"NULL AS {c}" for c in want)
        for r in conn.execute(
                f"""SELECT m.key_id, m.timestamp, m.message_type, {sel}
                    FROM message m JOIN message_media mm ON mm.message_row_id = m._id
                    WHERE m.key_id IS NOT NULL"""):
            fpath = r["file_path"]
            name = r["media_name"] or (Path(fpath).name if fpath else None)
            yield (r["key_id"], "msgstore", MEDIA_TYPES.get(r["message_type"], "file"),
                   hex_hash(r["file_hash"]), name, fpath, _int(r["file_size"]),
                   r["mime_type"], r["direct_path"], _b64(r["media_key"]),
                   _b64(r["enc_file_hash"]), _int(r["media_key_timestamp"]),
                   _int(r["timestamp"]), _int(r["width"]) or None, _int(r["height"]) or None)
    finally:
        conn.close()


def link_rows(kb) -> Iterator[tuple]:
    """Media descriptors the linked device sent: the `ref` in each archived media
    message (wa-link puts WhatsApp's CDN reference there)."""
    rows = kb.file.read_sync(lambda c: c.execute(
        "SELECT key, ts, media FROM messages WHERE kind = 'media' "
        "AND media LIKE '%\"ref\"%'").fetchall())
    for key, ts, raw in rows:
        try:
            media = json.loads(raw)
        except ValueError:
            continue
        ref = media.get("ref")
        if not isinstance(ref, dict):
            continue
        yield (key, "link", media.get("type"), hex_hash(ref.get("file_sha256")),
               media.get("name"), None, _int(ref.get("size")), ref.get("mime"),
               ref.get("direct_path"), ref.get("media_key"), ref.get("enc_file_hash"),
               None, ts, None, None)


def collect_meta(kb, *, msgstore: Path | None = None,
                 ctx: ImportContext | None = None) -> dict[str, int]:
    """Refresh `wa_media_meta` from the sources that exist. Idempotent."""
    counts: dict[str, int] = {}
    for source, rows in (("msgstore", msgstore_rows(msgstore) if msgstore and msgstore.is_file()
                          else iter(())),
                         ("link", link_rows(kb))):
        batch: list[tuple] = []

        def flush() -> None:
            if batch:
                kb.file.write_sync(lambda c: c.executemany(_UPSERT, batch))
                batch.clear()
        n = 0
        for row in rows:
            batch.append(row)
            n += 1
            if len(batch) >= 5000:
                if ctx is not None:
                    ctx.check()
                flush()
        flush()
        counts[source] = n
    return counts


# ---- 2. the folder matcher ------------------------------------------------------------------

def image_dims(p: Path) -> tuple[int, int] | None:
    """(width, height) from a JPEG, PNG or WebP header; None for anything else."""
    try:
        with p.open("rb") as f:
            d = f.read(1 << 18)
    except OSError:
        return None
    if d[:8] == b"\x89PNG\r\n\x1a\n" and len(d) >= 24:
        return struct.unpack(">II", d[16:24])
    if d[:4] == b"RIFF" and d[8:12] == b"WEBP":
        if d[12:16] == b"VP8X":
            return (1 + int.from_bytes(d[24:27], "little"), 1 + int.from_bytes(d[27:30], "little"))
        if d[12:16] == b"VP8 " and len(d) >= 30:
            w, h = struct.unpack("<HH", d[26:30])
            return (w & 0x3FFF, h & 0x3FFF)
        if d[12:16] == b"VP8L" and len(d) >= 25:
            b = int.from_bytes(d[21:25], "little")
            return (1 + (b & 0x3FFF), 1 + ((b >> 14) & 0x3FFF))
        return None
    if d[:2] != b"\xff\xd8":
        return None
    i = 2
    while i < len(d) - 9:
        if d[i] != 0xFF:
            i += 1
            continue
        marker = d[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD,
                      0xCE, 0xCF):
            h, w = struct.unpack(">HH", d[i + 5:i + 9])
            return (w, h)
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + struct.unpack(">H", d[i + 2:i + 4])[0]
    return None


def _same_dims(a: tuple[int, int] | None, b: tuple[int, int] | None) -> bool:
    return bool(a and b and (tuple(a) == tuple(b) or tuple(a) == tuple(b)[::-1]))


def _name_day(name: str) -> datetime | None:
    m = _WA_NAME.match(name)
    try:
        return datetime.strptime(m.group(2), "%Y%m%d") if m else None
    except ValueError:
        return None


def walk(folder: Path) -> list[Path]:
    out = []
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or p.name.lower() in _SKIP_NAMES:
            continue
        rel = p.relative_to(folder).parts
        if any(x.startswith(".") for x in rel):      # .Statuses, .trashed-..., .thumbnails
            continue
        out.append(p)
    return out


def _content_type(name: str, mime: str | None) -> str:
    return (mime or mimetypes.guess_type(name)[0] or "application/octet-stream").split(";")[0]


class _Index:
    """The archive's media messages and WhatsApp's metadata, for matching."""

    def __init__(self, conn: sqlite3.Connection):
        self.msgs: dict[str, list[dict[str, Any]]] = {}        # key -> rows
        self.by_name: dict[str, set[str]] = {}                 # recorded file name -> keys
        for r in conn.execute("SELECT id, chat_id, key, ts, media FROM messages "
                              "WHERE kind = 'media'"):
            try:
                media = json.loads(r["media"]) if r["media"] else {}
            except ValueError:
                media = {}
            self.msgs.setdefault(r["key"], []).append(
                {"id": r["id"], "chat_id": r["chat_id"], "ts": r["ts"], "media": media,
                 "type": media.get("type")})
            if media.get("name"):                           # a .txt export's attachment
                self.by_name.setdefault(media["name"].lower(), set()).add(r["key"])
        self.by_hash: dict[str, set[str]] = {}
        self.size: dict[str, int] = {}
        self.mime: dict[str, str] = {}
        self.dims: dict[str, tuple[int, int]] = {}
        for r in conn.execute("SELECT key, file_hash, file_name, file_size, mime, width, "
                              "height FROM wa_media_meta"):
            if r["file_hash"]:
                self.by_hash.setdefault(r["file_hash"], set()).add(r["key"])
            if r["file_name"]:
                self.by_name.setdefault(r["file_name"].lower(), set()).add(r["key"])
            if r["file_size"]:
                self.size[r["key"]] = r["file_size"]
            if r["mime"]:
                self.mime[r["key"]] = r["mime"]
            if r["width"] and r["height"]:
                self.dims[r["key"]] = (r["width"], r["height"])
        self.named = {k for keys in self.by_name.values() for k in keys}
        self.day_type: dict[tuple[str, str], list[str]] = {}   # (yyyymmdd, type) -> keys
        for key, rows in self.msgs.items():
            for m in rows:
                day = datetime.fromtimestamp(m["ts"] / 1000).strftime("%Y%m%d")
                self.day_type.setdefault((day, m["type"] or ""), []).append(key)

    def rows(self, keys: set[str] | list[str]) -> list[dict[str, Any]]:
        return [m for k in keys for m in self.msgs.get(k, [])]


def _has_file(m: dict[str, Any], sha: str | None = None) -> bool:
    files = m["media"].get("files") or []
    return any(f.get("sha256") and (sha is None or f["sha256"] == sha) for f in files)


def match_exact(ix: _Index, p: Path, sha: str, size: int
                ) -> tuple[str, list[dict[str, Any]], list[str]]:
    """(method, message rows, keys) by what WhatsApp recorded for the file: "hash",
    "name" or "resaved"; ("", [], []) when nothing matched."""
    keys = ix.by_hash.get(sha)
    if keys:
        rows = ix.rows(keys)
        if rows:
            return "hash", rows, sorted(keys)
    keys = ix.by_name.get(p.name.lower())
    if keys:
        # the phone names each file once (IMG-<day>-WA<n>); several messages with that
        # name are one file forwarded. A known size that differs is another file, unless
        # it is the same picture saved again (a gallery or sync app rewrote it): same
        # pixel size, and the name's day within a day of the message.
        same = {k for k in keys if ix.size.get(k) in (None, size)}
        rows = ix.rows(same)
        if rows:
            return "name", rows, sorted(same)
        dims = image_dims(p)
        day = _name_day(p.name)
        resaved = set()
        for k in keys:
            if not _same_dims(dims, ix.dims.get(k)):
                continue
            ts = [m["ts"] for m in ix.msgs.get(k, [])]
            if day is None or any(abs((datetime.fromtimestamp(t / 1000) - day).days) <= 1
                                  for t in ts):
                resaved.add(k)
        rows = ix.rows(resaved)
        if rows:
            return "resaved", rows, sorted(resaved)
    return "", [], []


def match_date(ix: _Index, p: Path) -> tuple[str, list[dict[str, Any]], list[str]]:
    """A guess for files WhatsApp's records do not know: the name's day and type, when
    exactly one message of that type that day still has no file and no recorded name
    (and, for a picture, the pixel size agrees when it is known). Runs after every
    exact match is in, and never for a name WhatsApp gave to another message."""
    m = _WA_NAME.match(p.name)
    if not m or p.name.lower() in ix.by_name:
        return "", [], []
    kinds = _NAME_TYPES[m.group(1).upper()]
    cands = [k for t in kinds for k in ix.day_type.get((m.group(2), t), [])]
    free = sorted({k for k in cands if k not in ix.named
                   and not any(_has_file(r) for r in ix.msgs.get(k, []))})
    if "photo" in kinds:
        dims = image_dims(p)
        free = [k for k in free if k not in ix.dims or _same_dims(dims, ix.dims[k])]
    if len(free) == 1:
        return "date", ix.rows(free), free
    return ("ambiguous", [], []) if free else ("", [], [])


def scan_folder(kb, folder: Path, *, dry_run: bool = True, by_date: bool = False,
                msgstore: Path | None = None,
                ctx: ImportContext | None = None) -> dict[str, Any]:
    """Match every file under `folder` to the archive's messages: exact matches first
    (hash, name, re-saved), then, with `by_date`, the day guess for what is left. A dry
    run changes nothing but `wa_media_meta` (it is refreshed first) and returns the
    report."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise FileNotFoundError(f"{folder} is not a folder on this machine")
    meta = collect_meta(kb, msgstore=msgstore, ctx=ctx)
    ix = kb.file.read_sync(_Index)
    files = walk(folder)
    rep: dict[str, Any] = {
        "dry_run": dry_run, "folder": str(folder), "meta": meta, "files": len(files),
        "bytes": 0, "matched": {"hash": 0, "name": 0, "resaved": 0, "date": 0},
        "ambiguous": 0, "unmatched": 0, "messages_linked": 0, "messages_already_had_it": 0,
        "copied": 0, "by_type": {}, "unmatched_examples": [],
        "ambiguous_examples": []}
    d = media_dir(kb)
    updates: dict[int, dict[str, Any]] = {}             # message id -> new media json
    progress = ctx.progress if ctx else (lambda *a: None)
    progress(0, len(files), "files")

    def link(p: Path, sha: str, size: int, method: str, rows, keys) -> None:
        rep["matched"][method] += 1
        t = rows[0]["type"] or "file"
        rep["by_type"][t] = rep["by_type"].get(t, 0) + 1
        rec = {"name": p.name, "size": size,
               "content_type": _content_type(p.name, next((ix.mime[k] for k in keys
                                                           if k in ix.mime), None)),
               "sha256": sha, "source": "folder", "match": method}
        linked = False
        for m in rows:
            if _has_file(m, sha):
                rep["messages_already_had_it"] += 1
                continue
            media = updates.get(m["id"]) or dict(m["media"])
            media["files"] = [*(media.get("files") or []), rec]
            updates[m["id"]] = media
            m["media"] = media                          # later files see this one
            rep["messages_linked"] += 1
            linked = True
        if linked and not dry_run:
            dest = d / sha
            if not dest.exists():
                d.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".tmp")
                shutil.copyfile(p, tmp)
                tmp.replace(dest)
                rep["copied"] += 1
            if len(updates) >= 500:
                _write_media(kb, updates)

    left: list[tuple[Path, str, int]] = []
    for i, p in enumerate(files):
        if ctx is not None and i % 50 == 0:
            ctx.check()
        size = p.stat().st_size
        rep["bytes"] += size
        sha = sha256_file(p)
        method, rows, keys = match_exact(ix, p, sha, size)
        if method:
            link(p, sha, size, method, rows, keys)
        else:
            left.append((p, sha, size))
        progress(i + 1, len(files), "files")
    for p, sha, size in left:
        method, rows, keys = match_date(ix, p) if by_date else ("", [], [])
        if method == "date":
            link(p, sha, size, method, rows, keys)
            continue
        rep["ambiguous" if method else "unmatched"] += 1
        ex = rep["ambiguous_examples" if method else "unmatched_examples"]
        if len(ex) < 20:
            ex.append(str(p.relative_to(folder)))
    if not dry_run:
        _write_media(kb, updates)
    rep["photo_messages_without_file"] = kb.file.read_sync(lambda c: c.execute(
        "SELECT COUNT(*) FROM messages WHERE kind = 'media' "
        "AND json_extract(media, '$.type') = 'photo' "
        "AND media NOT LIKE '%\"sha256\"%'").fetchone()[0])
    return rep


def _write_media(kb, updates: dict[int, dict[str, Any]]) -> None:
    if not updates:
        return
    rows = [(json.dumps(m, ensure_ascii=False), mid) for mid, m in updates.items()]
    kb.file.write_sync(lambda c: c.executemany("UPDATE messages SET media = ? WHERE id = ?",
                                               rows))
    updates.clear()


# ---- 3. wa-link: download by reference, media retry to the user's own phone -----------------

class MediaOff(Exception):
    """The feature is turned off in [media]; says which flag and why."""


EXPIRED = "expired on the CDN"
RETRY_POLL_SECONDS = 2.0     # how often to look for the files the phone was asked to send again


def downloadable(kb, limit: int = 100, types: tuple[str, ...] = ("photo",),
                 expired: bool = False) -> list[dict[str, Any]]:
    """Media messages without a file that WhatsApp's CDN may still serve, newest first,
    with the reference wa-link needs and who/where for a media retry. Messages tried
    before (`media_fetch`) are left out; with `expired`, the ones that were only
    expired come back (for a media retry)."""
    again = f"OR (f.state = 'gone' AND f.error = '{EXPIRED}')" if expired else ""
    marks = ",".join("?" * len(types))

    def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            f"""SELECT m.id, m.key, m.ts, m.from_me, c.key AS chat, c.kind AS chat_kind,
                       p.key AS sender, w.source, w.mtype, w.direct_path, w.media_key,
                       w.file_hash, w.enc_file_hash, w.file_size, w.mime
                FROM messages m JOIN wa_media_meta w ON w.key = m.key
                JOIN chats c ON c.id = m.chat_id
                LEFT JOIN people p ON p.id = m.sender_id
                LEFT JOIN media_fetch f ON f.message_id = m.id
                WHERE m.kind = 'media' AND json_extract(m.media, '$.type') IN ({marks})
                  AND m.media NOT LIKE '%"sha256"%'
                  AND w.direct_path IS NOT NULL AND w.media_key IS NOT NULL
                  AND (f.message_id IS NULL {again})
                ORDER BY m.ts DESC, w.source = 'link' DESC""", types).fetchall()
        out: dict[int, dict[str, Any]] = {}
        for r in rows:                      # one per message; the linked device's is newer
            out.setdefault(r["id"], dict(r))
            if len(out) >= limit:
                break
        return list(out.values())
    return kb.file.read_sync(run)


def _mark(kb, mid: int, state: str, error: str | None = None) -> None:
    kb.file.write_sync(lambda c: c.execute(
        "INSERT OR REPLACE INTO media_fetch (message_id, source, at, state, error) "
        "VALUES (?, 'wa-link', ?, ?, ?)", (mid, now_ms(), state, error)))


def _download(kb, link, m: dict[str, Any], direct_path: str | None = None
              ) -> tuple[str, str | None]:
    """("ok" | "gone" | "failed" | "offline", error). `link`: the WhatsApp connector
    (kb/connectors.py)."""
    ref = {"direct_path": direct_path or m["direct_path"], "media_key": m["media_key"],
           "file_hash": m["file_hash"], "enc_file_hash": m["enc_file_hash"],
           "mtype": m["mtype"],
           # wa-link refuses a file over its cap by this, before it downloads anything
           "file_length": m.get("file_size") or 0}
    try:
        r = link.raw_sync("POST", "/admin/media/download", ref)
    except Unreachable as e:
        return "offline", str(e)
    if r.status_code == 503:
        return "offline", "WhatsApp is not reachable."
    if r.status_code in (404, 410):
        return "gone", EXPIRED
    if r.status_code == 413:
        return "failed", r.json().get("error") or "too large"
    if r.status_code != 200 or not r.content:
        return "failed", f"{r.status_code} {r.text}"
    data = r.content
    sha = hashlib.sha256(data).hexdigest()
    if m["file_hash"] and sha != m["file_hash"]:
        return "failed", "hash mismatch"
    d = media_dir(kb)
    d.mkdir(parents=True, exist_ok=True)
    if not (d / sha).exists():
        tmp = (d / sha).with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(d / sha)
    _attach(kb, m["id"], {"name": None, "size": len(data),
                          "content_type": (m["mime"] or "").split(";")[0] or None,
                          "sha256": sha, "source": "wa-link"})
    return "ok", None


def request_media_retry(kb, settings, m: dict[str, Any], *, link) -> None:
    """Ask the user's OWN phone to upload one expired file again (wa-link
    /admin/media/retry: a media retry receipt to the own account; never to a contact).
    Needs `[media] wa_media_retry = true`. The answer is kept by wa-link
    (GET /admin/media/retry)."""
    if not settings.wa_media_retry:
        raise MediaOff("The app does not ask your phone for expired attachments "
                       "([media] wa_media_retry = false).")
    body = {"message_id": m["key"], "chat": m["chat"], "from_me": bool(m["from_me"]),
            "is_group": m["chat_kind"] == "group" or str(m["chat"]).endswith("@g.us"),
            "sender": "" if m["from_me"] else (m["sender"] or ""),
            "media_key": m["media_key"]}
    code, data = link.request_sync("POST", "/admin/media/retry", body)
    if code != 202:
        raise RuntimeError(f"The WhatsApp connection refused the attachment retry ({code}): "
                           f"{(data or {}).get('error') or ''}".strip())


def _retry_answers(link) -> dict[str, dict[str, Any]]:
    """The phone's answers so far, by WhatsApp message id."""
    try:
        code, data = link.request_sync("GET", "/admin/media/retry")
    except Unreachable:
        return {}
    return (data or {}).get("answers") or {} if code == 200 else {}


def _collect(kb, link, res: dict[str, Any]) -> int:
    """Download what the phone re-uploaded; returns how many requests still wait."""
    def waiting(c: sqlite3.Connection) -> list[dict[str, Any]]:
        return [dict(r) for r in c.execute(
            """SELECT m.id, m.key, w.mtype, w.media_key, w.file_hash, w.enc_file_hash, w.mime,
                      w.direct_path, w.file_size
               FROM media_fetch f JOIN messages m ON m.id = f.message_id
               JOIN wa_media_meta w ON w.key = m.key
               WHERE f.state = 'retry-requested'
               ORDER BY w.source = 'link' DESC""")]
    rows: dict[int, dict[str, Any]] = {}
    for r in kb.file.read_sync(waiting):
        rows.setdefault(r["id"], r)
    if not rows:
        return 0
    answers = _retry_answers(link)
    left = 0
    for m in rows.values():
        a = answers.get(m["key"])
        if a is None:
            left += 1
            continue
        if a.get("result") == "SUCCESS" and a.get("direct_path"):
            state, err = _download(kb, link, m, a["direct_path"])
            if state == "ok":
                res["retried_ok"] += 1
                _mark(kb, m["id"], "ok")
                continue
            if state == "offline":
                left += 1
                continue
            _mark(kb, m["id"], "failed", f"after the retry: {err}")
            res["failed"] += 1
        else:
            _mark(kb, m["id"], "gone", f"the phone: {a.get('result')} {a.get('error') or ''}".strip())
            res["gone"] += 1
    return left


def fetch_via_link(kb, settings, link, *, limit: int = 20, retry: bool = False,
                   ctx: ImportContext | None = None, sleep=time.sleep) -> dict[str, Any]:
    """Download photos without a file through wa-link, newest first. With `retry`, a
    photo whose CDN copy expired is asked from the user's own phone (at most
    `wa_retry_per_run`, `wa_retry_interval_seconds` apart), and the answers are awaited
    for `wa_retry_wait_seconds`; late answers are picked up by the next run."""
    if not settings.wa_download:
        raise MediaOff("The WhatsApp connection does not download attachments here "
                       "([media] wa_download = false).")
    if retry and not settings.wa_media_retry:
        raise MediaOff("The app does not ask your phone for expired attachments "
                       "([media] wa_media_retry = false).")
    res: dict[str, Any] = {"downloaded": 0, "gone": 0, "failed": 0, "retry_requested": 0,
                           "retried_ok": 0, "retry_waiting": 0, "stopped": None}
    _collect(kb, link, res)                             # answers to an earlier run
    todo = downloadable(kb, limit, expired=retry)
    for i, m in enumerate(todo):
        if ctx is not None:
            ctx.check()
            ctx.progress(i, len(todo), "photos")
        state, err = _download(kb, link, m)
        if state == "offline":
            res["stopped"] = err
            break
        if state == "ok":
            res["downloaded"] += 1
            _mark(kb, m["id"], "ok")
        elif state == "gone" and retry and \
                res["retry_requested"] < settings.wa_retry_per_run:
            if res["retry_requested"]:
                sleep(settings.wa_retry_interval_seconds)
            try:
                request_media_retry(kb, settings, m, link=link)
            except (RuntimeError, Unreachable) as e:
                _mark(kb, m["id"], "failed", str(e)[:200])
                res["failed"] += 1
                continue
            _mark(kb, m["id"], "retry-requested")
            res["retry_requested"] += 1
        else:
            _mark(kb, m["id"], state, err)
            res[state if state in ("gone", "failed") else "failed"] += 1
    deadline = time.monotonic() + (settings.wa_retry_wait_seconds
                                   if res["retry_requested"] else 0)
    left = _collect(kb, link, res)
    while left and time.monotonic() < deadline:
        if ctx is not None:
            ctx.check()
        sleep(RETRY_POLL_SECONDS)
        left = _collect(kb, link, res)
    res["retry_waiting"] = left
    return res


def _attach(kb, message_id: int, rec: dict[str, Any]) -> None:
    def run(c: sqlite3.Connection) -> None:
        row = c.execute("SELECT media FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            return
        media = json.loads(row[0]) if row[0] else {}
        if any(f.get("sha256") == rec["sha256"] for f in media.get("files") or []):
            return
        media["files"] = [*(media.get("files") or []), rec]
        c.execute("UPDATE messages SET media = ? WHERE id = ?",
                  (json.dumps(media, ensure_ascii=False), message_id))
    kb.file.write_sync(run)
