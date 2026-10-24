"""The notebook browser: the same files its tools see, for you. The notebook is its own,
the one notebook (her.notebook)."""
from __future__ import annotations

import difflib
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..her import Her
from ..errors import CelineError, NotFound, TooLarge
from ..skills import notebook_index
from ..skills.notebook import NO_SUMMARY, is_stamp, move_note, trash, unstamped, write_note
from .deps import cfg_of, store_of

router = APIRouter(prefix="/api")


def _who(request: Request) -> Her:
    """The agent's own is the one notebook."""
    return cfg_of(request).her


def _notebook_path(ch, rel: str) -> Path:
    root = ch.notebook.resolve()
    p = (root / rel).resolve()
    if rel in ("", ".") or not p.is_relative_to(root) or p == root:
        raise CelineError("bad_request", f"{rel!r} is outside {ch.name}'s notebook.", 400)
    # the app's own files (.index.db, .versions, .trash) are not notes, as for the tools
    if any(part.startswith(".") for part in p.relative_to(root).parts):
        raise CelineError("bad_request", f"{rel!r} is not a note.", 400)
    return p


def _virtual(request: Request) -> dict[str, str]:
    nb = request.app.state.services.registry.skills.get("notebook")
    return nb.virtual() if nb is not None else {}


def _refuse_virtual(rel: str) -> None:
    if rel == "tools" or rel.startswith("tools/"):
        raise CelineError("bad_request", "The app generates tools/. You cannot change it.", 400)


def _notebook_skill(request: Request):
    return request.app.state.services.registry.skills.get("notebook")


def _owner(request: Request, rel: str) -> str | None:
    nb = _notebook_skill(request)
    return nb.owner(rel) if nb is not None else None


def _mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


@router.get("/notebook/tree")
async def notebook_tree(request: Request) -> dict:
    """Every folder and note; a note with `updated_at` (ISO, UTC) and `line`, the summary
    its index keeps of it (null before it has one)."""
    ch = _who(request)
    nb = _notebook_skill(request)
    lines = {r["path"]: r["summary"] for r in await notebook_index.list(store_of(request))
             if r["summary"] and r["summary"] != NO_SUMMARY}
    if nb is not None:  # a placeholder named after the user lands once there is a name
        await nb.seed_one(store_of(request))
    root = ch.notebook

    def walk(d: Path, rel: str) -> list[dict]:
        out = []
        for p in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if p.name.startswith("."):
                continue
            r = f"{rel}/{p.name}" if rel else p.name
            if p.is_dir():
                out.append({"name": p.name, "path": r, "kind": "dir",
                            "children": walk(p, r)})
            else:
                owner = _owner(request, r)
                out.append({"name": p.name, "path": r, "kind": "file",
                            "bytes": p.stat().st_size, "protected": owner is not None,
                            "owner": owner, "updated_at": _mtime(p),
                            "line": lines.get(r)})
        return out

    tree = walk(root, "") if root.is_dir() else []
    virtual = _virtual(request)
    if virtual:
        tree.append({"name": "tools", "path": "tools", "kind": "dir", "virtual": True,
                     "children": [{"name": p.split("/", 1)[1], "path": p, "kind": "file",
                                   "bytes": len(c), "protected": True, "virtual": True}
                                  for p, c in virtual.items()]})
    return {"tree": tree}


@router.get("/notebook/entries/{path:path}")
async def notebook_read(request: Request, path: str) -> dict:
    """{path, content (the text as stored, for editing), shown (to read: its name, the
    user's and its pronouns filled in, the Last changed line left out, the head says
    it), protected, owner}."""
    cfg, ch = cfg_of(request), _who(request)
    if (text := _virtual(request).get(path)) is not None:
        return {"path": path, "content": text, "shown": text, "protected": True,
                "virtual": True}
    p = _notebook_path(ch, path)
    if not p.is_file():
        raise NotFound("note")
    try:
        text = p.read_text()
    except UnicodeDecodeError:
        raise CelineError("not_text", f"{path} is not a text file.", 415) from None
    return {"path": path, "content": text, "shown": cfg.substitute(unstamped(text)),
            "protected": _owner(request, path) is not None,
            "owner": _owner(request, path)}


class NotebookWrite(BaseModel):
    content: str


@router.put("/notebook/entries/{path:path}")
async def notebook_write(request: Request, path: str, body: NotebookWrite) -> dict:
    """The note's whole text."""
    cfg, ch = cfg_of(request), _who(request)
    _entry_path(path)
    if len(body.content.encode()) > cfg.r("max_message_bytes"):
        raise TooLarge("This note", len(body.content.encode()),
                       cfg.r("max_message_bytes"))
    _refuse_virtual(path)
    p = _notebook_path(ch, path)
    content = await write_note(cfg, store_of(request), ch, p, path, body.content)
    shown = cfg.substitute(unstamped(content))
    return {"ok": True, "bytes": len(content), "content": content, "shown": shown}


def _versions_dir(ch, rel: str) -> Path:
    return ch.notebook.resolve() / ".versions" / rel


def _version_time(stem: str) -> str | None:
    """The time in a version's name (`2026-01-09_141502_123456`) as ISO, UTC."""
    try:
        return datetime.strptime(stem, "%Y-%m-%d_%H%M%S_%f").replace(
            tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


PAGE = 50


def _change(old: str, new: str) -> str:
    """What changed from `old` to `new`, in one line: the first line added or changed,
    else the first line taken out; the Last changed line never counts."""
    a = [x for x in old.splitlines() if x.strip() and not is_stamp(x)]
    b = [x for x in new.splitlines() if x.strip() and not is_stamp(x)]
    added = removed = ""
    for op, i1, _, j1, _ in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("replace", "insert") and not added:
            added = b[j1]
        if op == "delete" and not removed:
            removed = a[i1]
    return (added or removed).strip().lstrip("#").strip()[:200]


def _history(ch, rel: str) -> list[dict]:
    """Every text the note had, oldest first, its text now last: {id (1, 2, ...), at
    (when it became the note's text), by, change, size, text}. The copies in
    .versions/<path>/ are named by when each was replaced."""
    p = _notebook_path(ch, rel)
    d = _versions_dir(ch, rel)
    kept = sorted((f for f in d.glob("*.md") if _version_time(f.stem)), key=lambda f: f.stem) \
        if d.is_dir() else []
    texts = [f.read_text(errors="replace") for f in kept]
    times = [_version_time(f.stem) for f in kept]
    if p.is_file():
        texts.append(p.read_text(errors="replace"))
        times.append(_mtime(p))
    out, before = [], ""
    for i, text in enumerate(texts):
        # a copy is named by when it was replaced: it became the text when the one
        # before it was replaced (the first: its own time, the best known)
        out.append({"id": i + 1, "at": times[i - 1] if i else times[0], "by": "",
                    "change": _change(before, text), "size": len(text), "text": text})
        before = text
    return out


@router.get("/notebook/versions")
async def notebook_versions(request: Request, path: str, q: str = "",
                            before: int | None = None) -> dict:
    """A note's History, as every long text's (web/src/lib/texts.ts): {versions: [{id, at,
    by, change, size}], current, total, next}. `versions` are the texts it had before,
    newest first, 50 a page, the next page `before` the last id; `current` is the text it
    has now, the newest of all (null when `q`, which looks in the changes, misses it or a
    page after the first is asked); `total` counts both."""
    # note: reads every copy on each page; an index once a note has thousands
    every = [{k: v for k, v in r.items() if k != "text"} for r in _history(_who(request), path)]
    now = every.pop() if every else None
    rows = [r for r in reversed(every)
            if (before is None or r["id"] < before) and q.lower() in r["change"].lower()]
    page = rows[:PAGE]
    show_now = now is not None and before is None and q.lower() in now["change"].lower()
    return {"versions": page, "current": now if show_now else None, "total": len(every) + (now is not None),
            "next": page[-1]["id"] if len(rows) > PAGE else None}


@router.get("/notebook/versions/{vid}")
async def notebook_version(request: Request, vid: int, path: str) -> dict:
    """One text the note had: {id, at, by, change, size, text}, as stored; Restore this
    version puts it into the editor and Save writes it."""
    found = next((r for r in _history(_who(request), path) if r["id"] == vid), None)
    if found is None:
        raise NotFound("version")
    return found


class NotebookPath(BaseModel):
    path: str = Field(min_length=1, max_length=400)


@router.post("/notebook/folders")
async def notebook_mkdir(request: Request, body: NotebookPath) -> dict:
    _refuse_virtual(body.path)
    p = _notebook_path(_who(request), body.path)
    if p.exists() and not p.is_dir():
        raise CelineError("bad_request", f"{body.path} is a file.", 400)
    p.mkdir(parents=True, exist_ok=True)
    return {"ok": True}


class NotebookChange(BaseModel):
    path: str | None = Field(default=None, min_length=1, max_length=400)
    protected: bool | None = None


@router.patch("/notebook/entries/{path:path}")
async def notebook_change(request: Request, path: str, body: NotebookChange) -> dict:
    """{path}: the note or folder moves there, its history and protection along;
    {protected}: write protection for it on a note (the seeded ones, skills/notebook.py
    SEEDS, stay). -> {ok, owner}."""
    _entry_path(path)
    ch = _who(request)
    nb = _notebook_skill(request)
    if body.path is not None:
        await _move(request, ch, nb, path, body.path)
        path = body.path
    if body.protected is not None:
        _refuse_virtual(path)
        if nb is None:
            raise NotFound("notebook")
        if nb.owner(path) == "config":
            raise CelineError("bad_request",
                              f"{path} is always read-only for {ch.name}.", 400)
        _notebook_path(ch, path)
        await nb.mark(store_of(request), path, body.protected)
    return {"ok": True, "owner": nb.owner(path) if nb is not None else None}


async def _move(request: Request, ch, nb, src_rel: str, dst_rel: str) -> None:
    _refuse_virtual(src_rel)
    _refuse_virtual(dst_rel)
    src, dst = _notebook_path(ch, src_rel), _notebook_path(ch, dst_rel)
    if not src.exists():
        raise NotFound("path")
    if dst.exists():
        raise CelineError("bad_request", f"{dst_rel} already exists.", 400)
    if src.is_dir() and dst.is_relative_to(src):
        raise CelineError("bad_request", "A folder cannot move into itself.", 400)
    move_note(ch.notebook.resolve(), src, dst, src_rel, dst_rel)
    await notebook_index.move(store_of(request), src_rel, dst_rel)
    if nb is not None:
        base = src_rel.rstrip("/")
        for rel in sorted(nb.marked):
            if rel == src_rel or rel.startswith(base + "/"):
                await nb.mark(store_of(request), rel, False)
                await nb.mark(store_of(request), dst_rel.rstrip("/") + rel[len(base):], True)


def _entry_path(path: str) -> None:
    if not path or len(path) > 400:
        raise CelineError("bad_request", "A notebook path is 1 to 400 characters.", 400,
                          {"field": "path"})


@router.delete("/notebook/entries/{path:path}")
async def notebook_delete(request: Request, path: str) -> dict:
    """The note or folder moves to .trash/<time>/<path>, as its notebook__delete does."""
    ch = _who(request)
    _refuse_virtual(path)
    p = _notebook_path(ch, path)
    if not p.exists():
        raise NotFound("path")
    trash(ch.notebook.resolve(), p, path)
    removed = await notebook_index.delete_prefix(store_of(request), path)
    return {"ok": True, "index_rows": removed}


def _trashed(root: Path) -> list[dict]:
    """What .trash holds, newest first: {id (its .trash folder), path (where it was),
    kind}. A delete moved one note or folder to .trash/<time>/<path>."""
    t = root / ".trash"
    out = []
    for d in sorted(t.iterdir(), reverse=True) if t.is_dir() else []:
        p, parts = d, []
        while p.is_dir() and len(kids := list(p.iterdir())) == 1:
            p = kids[0]
            parts.append(p.name)
        if d.is_dir() and parts:
            out.append({"id": d.name, "path": "/".join(parts),
                        "kind": "dir" if p.is_dir() else "file"})
    return out


@router.get("/notebook/trash")
async def notebook_trash(request: Request) -> dict:
    return {"trash": _trashed(_who(request).notebook.resolve())}


@router.post("/notebook/trash/{tid}/restore")
async def notebook_untrash(request: Request, tid: str) -> dict:
    """A deleted note or folder back where it was, indexed again."""
    ch = _who(request)
    root = ch.notebook.resolve()
    entry = next((e for e in _trashed(root) if e["id"] == tid), None)
    if entry is None:
        raise NotFound("deleted note")
    dst = _notebook_path(ch, entry["path"])
    if dst.exists():
        raise CelineError("bad_request", f"{entry['path']} exists again. Move it away first.",
                          400)
    src = root / ".trash" / tid / entry["path"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    # the folders it was in, empty now
    for d in [*src.parents][:len(Path(entry["path"]).parts)]:
        d.rmdir()
    for f in [dst] if dst.is_file() else sorted(x for x in dst.rglob("*") if x.is_file()):
        try:
            text = f.read_text()
        except UnicodeDecodeError:
            continue
        await notebook_index.upsert(store_of(request), f.relative_to(root).as_posix(),
                                    NO_SUMMARY, len(text), text)
    return {"ok": True, "path": entry["path"]}


@router.delete("/notebook/trash/{tid}")
async def notebook_purge(request: Request, tid: str) -> dict:
    """One deleted note or folder gone for good, for you: its .trash folder goes. The agent's
    own delete only ever moves a note to the trash."""
    root = _who(request).notebook.resolve()
    entry = next((e for e in _trashed(root) if e["id"] == tid), None)
    if entry is None:
        raise NotFound("deleted note")
    shutil.rmtree(root / ".trash" / tid)
    return {"ok": True, "path": entry["path"]}
