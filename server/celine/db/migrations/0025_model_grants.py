"""Who approved or set a cloud model, kept in the database:

- `model_grants`: a cloud model the user approved for code__start, served to the
  shellbox's model proxy until `until` (settings/agent_models.py grant_sync), so a restart
  of the app keeps it for the session still working in the box.
- every plan step gets `by` (plans/store.py): who set its model, "user" (the Missions
  page) or "her" (plan__set), None without a model. A model set before this is hers, so
  a switch to it asks as it did.

Run again, it writes the same."""
from __future__ import annotations

import json


def up(conn, db_path) -> None:
    conn.execute("BEGIN")
    conn.execute("CREATE TABLE IF NOT EXISTS model_grants "
                 "(ref TEXT PRIMARY KEY, until TEXT NOT NULL)")
    for cid, raw in conn.execute("SELECT conversation_id, steps FROM plans").fetchall():
        steps = [{**s, "by": s.get("by", "her" if s.get("model") else None)}
                 for s in json.loads(raw)]
        conn.execute("UPDATE plans SET steps=? WHERE conversation_id=?",
                     (json.dumps(steps, ensure_ascii=False), cid))
    conn.execute("COMMIT")
