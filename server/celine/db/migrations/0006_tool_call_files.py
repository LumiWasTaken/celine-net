"""Every tool call with files kept with it alone gets the `files` block its row draws
(attachments.with_files), as a call finished from now on does; a file its display names
already (a browser shot) stays out. A shell look's old `attachments` list goes: the block
replaces it. Run again, it writes the same."""
from __future__ import annotations

import json


def up(conn, db_path) -> None:
    conn.execute("BEGIN")
    calls = conn.execute(
        "SELECT t.id, t.display FROM tool_calls t WHERE t.id IN "
        "(SELECT tool_call_id FROM attachments WHERE tool_call_id IS NOT NULL)").fetchall()
    for call_id, raw in calls:
        display = json.loads(raw) if raw else {}
        display.pop("attachments", None)
        named = json.dumps({k: v for k, v in display.items() if k != "blocks"})
        files = [dict(zip(("id", "filename", "kind", "sha256", "bytes", "media_type",
                           "width", "height"), r)) for r in conn.execute(
            "SELECT a.id, a.filename, a.kind, a.sha256, b.bytes, b.media_type, b.width, "
            "b.height FROM attachments a JOIN blobs b ON b.sha256 = a.sha256 "
            "WHERE a.tool_call_id=? ORDER BY a.rowid", (call_id,))]
        files = [f for f in files if f'"{f["id"]}"' not in named]
        blocks = [b for b in display.get("blocks") or () if b.get("type") != "files"]
        if files:
            blocks.append({"type": "files", "where": "under", "files": files})
        display.pop("blocks", None)
        if blocks:
            display["blocks"] = blocks
        conn.execute("UPDATE tool_calls SET display=? WHERE id=?",
                     (json.dumps(display) if display else None, call_id))
    conn.execute("COMMIT")
