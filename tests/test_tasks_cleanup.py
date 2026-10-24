"""Tasks clean themselves up: a scheduled job is one state, never a list of runs, and
an error's detail stays in the server log."""
from __future__ import annotations

from chat_fixtures import until_sync

from celine.kb.tasks import Tasks


def wait(t):
    until_sync(lambda: t.state != "running", 2)


def test_quiet_runs_are_never_listed_and_leave_one_job_state():
    events = []
    ts = Tasks(events.append)

    def fail(ctx):
        raise RuntimeError("secret detail")
    wait(ts.start("w", "semantic", "update", lambda ctx: {"n": 1}, quiet=True))
    assert ts.list() == [] and not [e for e in events if e["type"] == "kb.task"]
    for _ in range(2):
        wait(ts.start("w", "semantic", "update", fail, quiet=True))
    assert ts.list() == []
    [job] = ts.jobs()
    assert job["state"] == "failed" and "secret detail" not in job["error"]
    wait(ts.start("w", "semantic", "update", lambda ctx: {}, quiet=True))
    assert ts.job("w", "semantic")["state"] == "done" and ts.job("w", "semantic")["error"] is None
