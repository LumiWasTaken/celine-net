"""Turn states (a live turn holds its conversation; a terminal one is over), and the
words a mission's, a tree node's and a run's state is said in: the one table the pages
and the events list read (`said`)."""
LIVE = ("queued", "preparing", "streaming", "tools")
TERMINAL = ("done", "aborted", "failed", "interrupted", "capped")
# for SQL: `state IN {LIVE_SQL}`
LIVE_SQL = "(" + ",".join(f"'{s}'" for s in LIVE) + ")"
# the order the queue is taken in (turns/store.py claim), `t` the turns table: the user's
# turns first, then the oldest; turns queued in one write go in the order they were made
QUEUE_ORDER = "t.origin != 'user', t.created_at, t.rowid"

# a state's tone, as the UI's StateWord takes it (ok: its success tone)
RUNNING, OK, ERROR, MUTED, WARN = "running", "ok", "error", "muted", "warn"
# a state as (word, tone)
MISSION = {"active": ("Running", RUNNING), "needs_you": ("Waiting for you", WARN),
           "done": ("Done", OK), "stopped": ("Stopped", MUTED)}
# a node of a tree: a sub-agent (queued, working, done, failed, paused) or a mission
TREE = {**MISSION, "queued": ("Queued", RUNNING), "working": ("Running", RUNNING),
        "failed": ("Failed", ERROR), "paused": ("Paused", WARN)}
# a run; planned: a schedule's slot of the day with no run yet
RUN = {"waiting": ("Queued", MUTED), "running": ("Running", RUNNING), "done": ("Done", OK),
       "failed": ("Failed", ERROR), "skipped": ("Skipped", MUTED),
       "stopped": ("Stopped", MUTED), "planned": ("Scheduled", MUTED)}


def said(table: dict, state: str) -> dict:
    """{words, tone} of `state` in `table`."""
    words, tone = table[state]
    return {"words": words, "tone": tone}


def table(states: dict) -> dict:
    """A whole table as a response carries it: {state: {words, tone}}."""
    return {state: said(states, state) for state in states}
