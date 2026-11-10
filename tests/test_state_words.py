"""A state is said in the server's words (core/states.py): a mission's, a tree node's and
a run's rows carry `words` and `tone`, and a held run reads Queued."""
from __future__ import annotations

from celine.boot import load
from celine.core.states import MISSION, RUN, TREE, said, table
from celine.schedules.runs import view


def test_every_state_has_its_words_and_tone():
    assert said(MISSION, "needs_you") == {"words": "Waiting for you", "tone": "warn"}
    assert said(TREE, "working") == {"words": "Running", "tone": "running"}
    assert set(table(TREE)) >= set(MISSION), "a mission is a node of a tree too"
    assert table(RUN)["planned"] == {"words": "Scheduled", "tone": "muted"}
    tones = {tone for t in (MISSION, TREE, RUN) for _, tone in t.values()}
    assert tones <= {"running", "ok", "error", "muted", "warn"}


def test_a_run_says_its_state_and_a_held_one_reads_queued():
    row = {"id": "r1", "schedule": "world", "label": "World", "slot": "2026-10-10T02:00:00+00:00",
           "night": "2026-10-09", "state": "running", "reason": "", "conversation_id": None,
           "result": None, "error": None, "created_at": "", "started_at": None,
           "finished_at": None, "started_by": "clock", "again_of": None, "again_slot": None,
           "retried": None, "retried_slot": None}
    cfg = load()
    assert (view(row, cfg)["words"], view(row, cfg)["tone"]) == ("Running", "running")
    held = view(row, cfg, held=True)
    assert (held["state"], held["words"], held["tone"]) == ("waiting", "Queued", "muted")
