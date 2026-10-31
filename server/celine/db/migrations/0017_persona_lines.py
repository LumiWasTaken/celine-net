"""Her stored persona (the `her` row, config/her/persona.md and owner.md when it was
seeded) gets what the shipped one says now: the notebook paragraph that owner.md says
already leaves, and two lines join "How you work" after the one on a failed tool: work of
many steps goes to an agent or a drone, and she sets her status when she starts and
finishes. A persona without these lines stays as it is; run again, it writes nothing."""
from __future__ import annotations

import re

DUPLICATE = ("\n\nWhen you learn something durable about {{username}}, write it in your "
             "notebook. When you form an opinion, write it there too, with keys so it comes "
             "back. Who you are is in identity.md in your notebook.")
ADDED = ("- Work that takes many steps goes to an agent or a drone (`sub_agents`), and you "
         "keep your chat for {{username}}.\n"
         "- When you start on something and when it is done, set your status "
         "(`mind__set_status`).\n")
FAILED_TOOL = re.compile(r"^- If a tool fails.*\n", re.M)


def changed(persona: str) -> str:
    out = persona.replace(DUPLICATE, "")
    if "mind__set_status" not in out and (m := FAILED_TOOL.search(out)):
        out = out[:m.end()] + ADDED + out[m.end():]
    return out


def up(conn, db_path) -> None:
    from celine import texts
    row = conn.execute("SELECT persona FROM her WHERE id = 1").fetchone()
    if row is None or (new := changed(row[0])) == row[0]:
        return
    conn.execute("BEGIN")
    conn.execute("UPDATE her SET persona=? WHERE id = 1", (new,))
    texts.keep(conn, "her:persona", row[0], new, "")
    conn.execute("COMMIT")
