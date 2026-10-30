"""The tail of a turn is stored once on the user message it answers
(`message_context.tail`, turns/message_context.py; `text` NULL: no block worked out
yet, so a tail written first never stands for one), and notes are no longer carried by
need: `context_loaded` and `context_state.turns` go, and so do the `notes` share and
`idle_turns` in settings/context.toml (beside celine.db). Run again, it writes the same."""
from __future__ import annotations

import re
from pathlib import Path


def up(conn, db_path) -> None:
    conn.execute("BEGIN")
    if "tail" not in {r[1] for r in conn.execute("PRAGMA table_info(message_context)")}:
        conn.execute("CREATE TABLE message_context_new ("
                      "message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,"
                      " text TEXT, report TEXT NOT NULL DEFAULT '{}', tail TEXT)")
        conn.execute("INSERT INTO message_context_new(message_id, text, report) "
                     "SELECT message_id, text, report FROM message_context")
        conn.execute("DROP TABLE message_context")
        conn.execute("ALTER TABLE message_context_new RENAME TO message_context")
    conn.execute("DROP TABLE IF EXISTS context_loaded")
    if "turns" in {r[1] for r in conn.execute("PRAGMA table_info(context_state)")}:
        conn.execute("ALTER TABLE context_state DROP COLUMN turns")
    conn.execute("COMMIT")
    path = Path(db_path).parent / "settings" / "context.toml"
    if path.is_file():
        rx = re.compile(r"\s*(notes|idle_turns)\s*=")
        lines = path.read_text().splitlines(keepends=True)
        kept = [ln for ln in lines if not rx.match(ln)]
        if kept != lines:
            path.write_text("".join(kept))
