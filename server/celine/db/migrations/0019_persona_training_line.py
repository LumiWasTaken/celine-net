"""Her stored persona (the `her` row, seeded once from config/her/persona.md) gets the
line the shipped one has now in "How you work", after the one on her status: what she
knows about models, tools and versions stops at her training (she looks up what is
current when unsure). Inserted only after that exact line; a persona without it, or
with the new line already, stays as it is."""
from __future__ import annotations

STATUS = ("- When you start on something and when it is done, set your status "
          "(`mind__set_status`).\n")
ADDED = ("- What you know about models, tools and their versions stops at your training. "
         "When you are not sure what is current, look it up on the web first.\n")


def changed(persona: str) -> str:
    if ADDED in persona or persona.count(STATUS) != 1:
        return persona
    return persona.replace(STATUS, STATUS + ADDED)


def up(conn, db_path) -> None:
    from celine import texts
    row = conn.execute("SELECT persona FROM her WHERE id = 1").fetchone()
    if row is None or (new := changed(row[0])) == row[0]:
        return
    conn.execute("BEGIN")
    conn.execute("UPDATE her SET persona=? WHERE id = 1", (new,))
    texts.keep(conn, "her:persona", row[0], new, "")
    conn.execute("COMMIT")
