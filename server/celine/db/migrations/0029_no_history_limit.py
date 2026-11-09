"""A request carries every message since the chat's newest summary (turns/context.py);
the sliding limit `history_messages` is gone, and its saved value leaves
settings/general.toml (beside celine.db). A chat that limit had cut keeps the view it
has: one compactions row at that cut, with the text and the count of the summary it had
before (empty without one). No model is asked, and the chat's next summary starts there.
Run again, it writes nothing."""
from __future__ import annotations

import tomllib
from pathlib import Path

DEFAULT = 200       # the limit of a file that never saved one


def _limit(db_path) -> int:
    """The limit in force, and the saved value taken out of the file."""
    from celine.settings.files import write_toml
    path = Path(db_path).parent / "settings" / "general.toml"
    if not path.is_file():
        return DEFAULT
    with path.open("rb") as f:
        data = tomllib.load(f)
    if "history_messages" not in data:
        return DEFAULT
    limit = data.pop("history_messages")
    write_toml(path, data)
    return int(limit) if isinstance(limit, int) and limit > 0 else 0


def up(conn, db_path) -> None:
    from celine.core.ids import new_id, now
    limit = _limit(db_path)
    if not limit:
        return
    chats = conn.execute("SELECT id, active_leaf_id FROM conversations "
                         "WHERE active_leaf_id IS NOT NULL").fetchall()
    conn.execute("BEGIN")
    for cid, leaf in chats:
        chain = []                       # the visible path, oldest first
        mid = leaf
        while mid:
            r = conn.execute(
                "SELECT m.id, m.parent_id, m.role, m.content, "
                "EXISTS (SELECT 1 FROM tool_calls t WHERE t.message_id = m.id), "
                "EXISTS (SELECT 1 FROM attachments a WHERE a.message_id = m.id) "
                "FROM messages m WHERE m.id=?", (mid,)).fetchone()
            if r is None:
                break
            chain.append(r)
            mid = r[1]
        chain.reverse()
        at = {r[0]: i for i, r in enumerate(chain)}
        last = None                      # its newest summary on this path
        for c in conn.execute("SELECT through_message_id, covers_messages, text, tokens "
                              "FROM compactions WHERE conversation_id=? "
                              "ORDER BY created_at, rowid", (cid,)):
            if c[0] in at:
                last = c
        start = at[last[0]] + 1 if last else 0
        # what a request counted: every message but a tool result and an empty one
        items = [i for i in range(start, len(chain))
                 if chain[i][2] != "tool" and (chain[i][3] or chain[i][4] or chain[i][5])]
        if len(items) <= limit:
            continue
        cut = len(items) - limit
        while cut < len(items) and chain[items[cut]][2] != "user":
            cut += 1
        if cut >= len(items) or items[cut] == 0:
            continue
        conn.execute(
            "INSERT INTO compactions(id, conversation_id, through_message_id, trigger, "
            "covers_messages, text, tokens, created_at) VALUES(?,?,?,'auto',?,?,?,?)",
            (new_id(), cid, chain[items[cut] - 1][0], last[1] if last else 0,
             last[2] if last else "", last[3] if last else 0, now()))
    conn.execute("COMMIT")
