"""celine doctor, celine export, celine purge, celine keys."""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import config as config_mod
from . import keys as keys_mod
from .attachments import blob_path
from .conversations import tree
from .core.states import LIVE_SQL
from .core.store import connect
from .db.migrate import read_only, schema_version


def _say(ok: bool, text: str) -> bool:
    print(f"  {'ok  ' if ok else 'WARN'} {text}")
    return ok


def keys(data_dir: Path) -> int:
    """The stack's keys under <data_dir>/keys, made when missing (keys.py)."""
    made = keys_mod.ensure(data_dir)
    print(f"keys: {data_dir / 'keys'}: " + (", ".join(made) + " made" if made else "all there"))
    return 0


def doctor(cfg: config_mod.Config) -> int:
    good = True
    print("config")
    for p in cfg.problems:
        good &= _say(not p.fatal, f"{p.file}: {p.message}")
    if not cfg.problems:
        _say(True, "no problems")

    print("database")
    if cfg.db_path.exists():
        with read_only(cfg.db_path) as c:
            good &= _say(c.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                         "integrity check")
            _say(True, f"schema version {schema_version(c)}")
            n = c.execute("SELECT count(*) FROM conversations").fetchone()[0]
            live = c.execute(f"SELECT count(*) FROM turns WHERE state IN {LIVE_SQL}"
                             ).fetchone()[0]
            _say(True, f"{n} conversations, {live} turns live")
    else:
        _say(True, "no database yet")
    free = shutil.disk_usage(cfg.data_dir if cfg.data_dir.exists() else Path.cwd()).free
    good &= _say(free > 500_000_000, f"{free / 1e9:.1f} GB free under {cfg.data_dir}")

    print("models")
    from .backends.router import app_router
    from .settings.registry import ROLES
    router = app_router(cfg)
    providers = cfg.settings.providers
    if not providers:
        _say(True, "no providers yet: add one in Settings, Models")
    for pid, p in providers.items():
        st = router.probe_sync(pid, max_age=0)
        if st.reachable:
            loaded = f", loaded now: {', '.join(sorted(st.loaded))}" if st.loaded else ""
            _say(True, f"{p.get('label', pid)}: reachable{loaded}")
        else:
            good &= _say(False, f"{p.get('label', pid)}: {st.error or 'not reachable'}")
    for role, info in ROLES.items():
        o = router.order_sync(role)
        if not o.chain:
            _say(True, f"{info['label']}: off")
        elif o.refs:
            _say(True, f"{info['label']}: {' then '.join(o.refs)}")
        else:
            good &= _say(False, f"{info['label']}: {o.sentence()}")

    print("sources")
    kb_dir = cfg.data_dir / "kb"
    for kid, block in cfg.kb.items():
        path = kb_dir / f"{kid}.db"
        if block.get("kind") not in ("chat", "documents"):
            good &= _say(False, f"{kid}: unknown kind {block.get('kind')!r}")
            continue
        if not path.exists():
            _say(True, f"{kid}: {block.get('kind')}, empty (no file yet)")
            continue
        with read_only(path) as c:
            ok = c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            if block.get("kind") == "chat":
                n = c.execute("SELECT COALESCE(SUM(message_count), 0) FROM chats").fetchone()[0]
                has = c.execute("SELECT 1 FROM sqlite_master WHERE name = 'passages'").fetchone()
                e = c.execute("SELECT COUNT(*), COALESCE(SUM(embedded), 0) FROM passages"
                              ).fetchone() if has else (0, 0)
                what = f"{n:,} messages, {e[1]:,} of {e[0]:,} passages embedded"
            else:
                n = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                what = f"{n} file{'' if n == 1 else 's'} in Documents"
            good &= _say(ok, f"{kid}: {what}{'' if ok else ', FAILED quick_check'}")
    for ref in router.order_sync("embeddings").refs[:1]:
        bc = router.backend_config(ref)
        try:
            r = httpx.post(f"{bc.base_url}/embeddings", json={"model": bc.model, "input": ["ok"]},
                           headers={"Authorization": f"Bearer {bc.api_key}"} if bc.api_key else {},
                           timeout=30)
            ok = r.status_code == 200
            dims = len(r.json()["data"][0]["embedding"]) if ok else 0
            good &= _say(ok, f"embeddings {ref} -> {f'{dims} dimensions' if ok else r.status_code}")
        except (httpx.HTTPError, ValueError, KeyError) as e:
            good &= _say(False, f"embeddings {ref}: no answer within 30 s ({type(e).__name__})")

    print("features")
    import anyio
    from .settings import features
    probes = anyio.run(features.probe_all, cfg, None)
    for f in features.catalog():
        state = "on" if features.on(cfg, f.id) else "off"
        pr = probes[f.id]
        said = f"{f.id}: {state}" + (f", {pr['state']}: {pr['detail']}" if pr["detail"] else "")
        good &= _say(state == "off" or pr["state"] != "missing", said)

    print("skills")
    for name in cfg.skills:
        _say(True, f"{name}: {'on' if cfg.skill_enabled(name) else 'off'}")
    print("mcp")
    if not cfg.mcp:
        _say(True, "no servers configured")
    for s in cfg.mcp.values():
        if not s.enabled:
            _say(True, f"{s.id}: off")
        elif s.transport == "stdio":
            found = bool(s.command and shutil.which(s.command))
            good &= _say(found, f"{s.id}: {s.command} {'found' if found else 'not on PATH'}")
        elif s.kb:
            good &= _say(*_connector(s))
        else:
            _say(True, f"{s.id}: {s.url}")
    print("triggers")
    on = [n for n, t in cfg.settings.triggers.items() if t.get("on")]
    _say(True, ", ".join(on) if on else "none on")
    print("\nall good" if good else "\nsomething needs a look")
    return 0 if good else 1


def _connector(s) -> tuple[bool, str]:
    """A connector's own word on its connection (GET /admin/status)."""
    from .kb.connectors import base_url
    try:
        r = httpx.get(f"{base_url(s.url)}/admin/status", headers=s.headers, timeout=5)
    except httpx.HTTPError:
        return False, f"{s.id}: the connection is not running at {s.url}"
    if r.status_code != 200:
        return False, f"{s.id}: the connection answered {r.status_code} (is its clients file the app's keys/connector-clients?)"
    conn = r.json().get("connection") or {}
    state = conn.get("state") or "unknown"
    return state != "needs_login", f"{s.id}: {state}" + (f", {conn['error']}"
                                                            if conn.get("error") else "")


def export(cfg: config_mod.Config, out: Path | None) -> Path:
    """Every conversation: full JSON of all versions, plus the active path as Markdown.
    By default into the Documents folder `exports/`, so Celine can read it too (the data
    directory without Documents)."""
    from .kb import documents_root
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    docs = documents_root(cfg)
    out = out or (docs / "exports" if docs is not None else cfg.data_dir) / f"export-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    c = connect(cfg.db_path)
    try:
        convs = [dict(r) for r in c.execute(
            "SELECT id, title, pinned, trigger_key, created_at, updated_at FROM conversations "
            "ORDER BY created_at")]
        for conv in convs:
            conv["messages"] = [dict(r) for r in c.execute(
                "SELECT id, parent_id, role, content, reasoning, ordinal, author, "
                "created_at FROM messages WHERE conversation_id=? ORDER BY created_at",
                (conv["id"],))]
            conv["tool_calls"] = [dict(r) for r in c.execute(
                "SELECT tc.id, tc.message_id, tc.name, tc.arguments, tc.state "
                "FROM tool_calls tc JOIN messages m ON m.id = tc.message_id "
                "WHERE m.conversation_id=?", (conv["id"],))]
            conv["attachments"] = [dict(r) for r in c.execute(
                "SELECT a.id, a.message_id, a.filename, a.kind, a.sha256, b.bytes, b.media_type "
                "FROM attachments a JOIN blobs b ON b.sha256 = a.sha256 "
                "JOIN messages m ON m.id = a.message_id WHERE m.conversation_id=?",
                (conv["id"],))]
            try:
                path = tree.path(c, conv["id"])
            except KeyError:
                path = []
            slug = "".join(ch if ch.isalnum() else "-" for ch in (conv["title"] or "untitled"))[:60]
            lines = [f"# {conv['title'] or 'Untitled'}", "",
                     f"created {conv['created_at']}", ""]
            for m in path:
                if m["role"] == "system":
                    continue
                who = {"user": cfg.user, "assistant": cfg.name, "tool": "tool"}[m["role"]]
                lines += [f"## {who}", "", m["content"] or "", ""]
            (out / f"{conv['created_at'][:10]}-{slug}-{conv['id'][:8]}.md").write_text(
                "\n".join(lines))
        (out / "conversations.json").write_text(json.dumps(convs, ensure_ascii=False, indent=1))
        for b in c.execute("SELECT sha256 FROM blobs"):
            src = blob_path(cfg, b["sha256"])
            if src.exists():
                (out / "blobs").mkdir(exist_ok=True)
                shutil.copy2(src, out / "blobs" / b["sha256"])
        from . import her
        nb = her.notebook(cfg)              # the one notebook
        if nb.exists():
            shutil.copytree(nb, out / "notebook", dirs_exist_ok=True)
    finally:
        c.close()
    print(f"exported {len(convs)} conversations to {out}", file=sys.stderr)
    return out


def purge(cfg: config_mod.Config, under: int, dry_run: bool) -> int:
    """In every chat source: delete and ignore each chat with fewer than `under`
    messages (as "Delete and ignore" does), then delete the
    people nothing refers to any more. People in a person record (a same person, or
    one with about or aliases) and people you named stay."""
    from .kb import KBs
    from .kb.chat import LISTED_MIN_MESSAGES
    from .kb.connectors import Connectors
    from .kb.identity import kept_sync
    from .kb.connectors import Refused
    from .kb.ignore import delete_and_ignore
    from .kb.importers import Unreachable

    kept: set[str] = set()
    if cfg.db_path.exists():
        with read_only(cfg.db_path) as c:
            kept = kept_sync(c)
    kbs = KBs(cfg, lambda ev: None)
    connectors = Connectors(cfg, kbs, lambda ev: None)
    for sid in cfg.mcp:
        connectors.open(sid)
    good = True
    for kb in kbs.chats():
        print(f"{kb.id} ({kb.title})")
        conn = connectors.get(kb.id)
        rows = kb.file.read_sync(lambda c: c.execute(
            "SELECT id, key, kind, message_count FROM chats WHERE message_count < ?",
            (under,)).fetchall())
        ids = {r["id"] for r in rows}
        keep = {int(r.split(":", 1)[1]) for r in kept if r.startswith(f"{kb.id}:")}
        kinds = {k: sum(r["kind"] == k for r in rows) for k in ("dm", "group", "channel")}
        print(f"  chats under {under} messages: {len(rows)} ({kinds['dm']} DMs, "
              f"{kinds['group']} groups, {kinds['channel']} channels), "
              f"{sum(r['message_count'] for r in rows)} messages")
        if not dry_run and ids:
            try:
                out = delete_and_ignore(kb, sorted(ids), conn)
            except (Unreachable, Refused) as e:
                print(f"  skipped: {e}")
                good = False
                continue
            print(f"  deleted and ignored {len(out['chats'])} chats, "
                  f"{out['messages']} messages")
        unused = kb.unused_people(keep, without_chats=ids if dry_run else set())
        n = len(unused) if dry_run else kb.delete_people(unused)
        print(f"  people {'to delete' if dry_run else 'deleted'}: {n} "
              f"(no message and no chat left)")
        few = kb.file.read_sync(lambda c: c.execute(
            """SELECT COUNT(*) FROM (SELECT m.sender_id FROM messages m
                 JOIN people p ON p.id = m.sender_id
                 WHERE p.is_me = 0 AND m.chat_id NOT IN (SELECT value FROM json_each(?))
                 GROUP BY m.sender_id HAVING COUNT(*) < ?) s
               WHERE s.sender_id NOT IN (SELECT value FROM json_each(?))""",
            (json.dumps(sorted(ids) if dry_run else []), LISTED_MIN_MESSAGES,
             json.dumps(sorted(keep)))).fetchone()[0])
        print(f"  people kept but left out of the people lists: {few} "
              f"(1 to {LISTED_MIN_MESSAGES - 1} messages, not in a person record)")
    print("dry run: nothing changed" if dry_run else "done")
    return 0 if good else 1
