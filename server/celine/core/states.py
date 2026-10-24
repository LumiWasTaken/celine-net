"""Turn states. A live turn holds its conversation; a terminal one is over."""
LIVE = ("queued", "preparing", "streaming", "tools")
TERMINAL = ("done", "aborted", "failed", "interrupted", "capped")
# for SQL: `state IN {LIVE_SQL}`
LIVE_SQL = "(" + ",".join(f"'{s}'" for s in LIVE) + ")"
# the order the queue is taken in (turns/store.py claim), `t` the turns table: the user's
# turns first, then the oldest; turns queued in one write go in the order they were made
QUEUE_ORDER = "t.origin != 'user', t.created_at, t.rowid"
