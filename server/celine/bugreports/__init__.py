"""The agent's bug reports: app problems it files any time, each its own Intercom thread.

- store.py: the rows (table bugreports): filed (its thread opens to the user from the
  conversation that filed it), listed, closed.
- skill.py: its tool, bugreport__file (guides/bugreport.md).
- api.py: GET /api/bugreports, POST /api/bugreports/{id}/close (the thread's Close).

The user's reply in a report's thread reaches it like any intercom reply; Continue in a
chat (intercom/api.py) takes the thread into a new chat with it. Off with `[bugreport]
enabled = false` in config/skills.toml: no tool. Events lists the reports as kind
`bugreport` (events.py)."""
