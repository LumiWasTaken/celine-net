"""Image captions as searchable text. Tiny on purpose: kb/instant.py and kb/passages.py
import it, so it imports nothing of theirs.

A message's files are `media.files[].sha256` (Discord downloads, matched WhatsApp
files). A caption belongs to a file (`media_captions`, keyed by sha256, see
kb/media.py), so the same photo forwarded to three chats is described once and found
in all three.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Mapping

from .db import in_batches

CAPTION_TABLE = "media_captions"


def parse(raw: str | None) -> dict | None:
    """A message's media column as a dict; None when it has none or it does not read.
    The one parser of that JSON."""
    if not raw:
        return None
    try:
        m = json.loads(raw)
    except ValueError:
        return None
    return m if isinstance(m, dict) else None


def file_shas(media: dict | None) -> list[str]:
    """sha256 of every downloaded file of a message, from its media (parsed)."""
    files = (media or {}).get("files")
    return [f["sha256"] for f in files if isinstance(f, dict)
            and isinstance(f.get("sha256"), str)] if isinstance(files, list) else []


def _lookup(conn: sqlite3.Connection, shas: list[str]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for part in in_batches(shas):
        marks = ",".join("?" * len(part))
        for r in conn.execute(
                f"SELECT sha256, caption, visible_text FROM {CAPTION_TABLE} "
                f"WHERE state = 'done' AND sha256 IN ({marks})", part):
            out[r[0]] = (r[1] or "", r[2] or "")
    return out


def for_rows(conn: sqlite3.Connection, rows: Iterable[Mapping[str, Any]], *,
             short: bool = False) -> dict[int, str]:
    """{message id: caption text} for the rows (each has `id` and `media`) whose files
    have a caption. `short`: the caption plus the visible text on one line, for a
    passage line; else caption and all visible text, for the instant index."""
    want: dict[int, list[str]] = {}
    for r in rows:
        raw = r["media"]
        shas = file_shas(parse(raw)) if raw and '"sha256"' in raw else []
        if shas:
            want[r["id"]] = shas
    if not want:
        return {}
    caps = _lookup(conn, sorted({s for v in want.values() for s in v}))
    if not caps:
        return {}
    out: dict[int, str] = {}
    for mid, shas in want.items():
        parts = []
        for s in shas:
            if s not in caps:
                continue
            cap, vis = caps[s]
            if short:
                vis = " ".join(vis.split())
                parts.append(f'{cap} (text: "{vis}")' if vis else cap)
            else:
                parts.append(f"{cap} {vis}".strip())
        if parts:
            out[mid] = "; ".join(p for p in parts if p)
    return out
