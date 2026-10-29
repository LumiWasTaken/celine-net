"""Tools: every call gets a result, a tool turned off never runs, and the notebook keeps
what it and the user wrote."""
from __future__ import annotations

from chat_fixtures import user_says

import pytest
from types import SimpleNamespace

from celine.conversations import store as conversations, view
from celine.boot import load
from celine.core.bus import EventBus
from celine.skills import Registry, ToolContext
from celine.skills.gating import is_enabled
from celine.skills.notebook import Notebook, build_index_fragment
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from celine.turns import store as turns
from celine.skills import notebook_index
from celine.skills.gating import overrides_for
from celine.skills.gating import set_override
from jobs_fixtures import two_shot

pytestmark = pytest.mark.anyio


@pytest.fixture
def cfg():
    c = load()
    c.skills = dict(c.skills)
    return c


@pytest.fixture
def registry(cfg):
    r = Registry()
    r.add(Notebook(cfg))
    return r


def ctx(store, cfg):
    return ToolContext(conversation_id="c", turn_id="t", tool_call_id="x", store=store,
                       config=cfg, emit=None)


async def _one_call(store, cfg, registry, name, arguments):
    cid = await conversations.create(store)
    await user_says(store, cid, "go")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, cfg, two_shot(name, arguments), EventBus(), Token(turn["id"]),
                   registry)
    events = [e for e in (await view.resume(store, cid, 0))["events"]
              if e["type"] in ("tool.done", "tool.gated")]
    return cid, events


async def test_every_failure_path_still_writes_a_result(store, cfg, registry):
    for name, args in [("no_such__tool", "{}"), ("notebook__write", "{not json"),
                       ("notebook__read", '{"nope": 1}'), ("notebook__read", '{"path": "x.md"}')]:
        cid, events = await _one_call(store, cfg, registry, name, args)
        assert len(events) == 1, name
        snap = await view.snapshot(store, cid)
        assert [m["role"] for m in snap["messages"]].count("tool") == 1
        assert snap["turn"]["state"] == "done"


async def test_an_override_takes_the_newest_seq_and_a_whole_skill(store, cfg, registry, monkeypatch):
    """A tool turned off never runs: test_skills; here an older seq and a whole skill."""
    cid = await conversations.create(store)
    await set_override(store, cid, "notebook__write", False, 5)
    assert await set_override(store, cid, "notebook__write", True, 3) is None, "an older seq"
    over = await overrides_for(store, cid)
    assert not is_enabled(registry.get("notebook__write"), over, cfg)
    assert is_enabled(registry.get("notebook__read"), over, cfg)
    await set_override(store, cid, "notebook", False, 6)          # the whole skill
    over = await overrides_for(store, cid)
    assert not any(is_enabled(s, over, cfg) for s in registry.tools.values()
                   if s.skill == "notebook")
    assert over == {"notebook": False}, "the skill's switch clears the tool rows under it"
    # on, it turns on a tool that is off in Settings too
    monkeypatch.setattr(cfg, "settings", SimpleNamespace(tools={"notebook__write": "off"}))
    assert not is_enabled(registry.get("notebook__write"), {}, cfg)
    await set_override(store, cid, "notebook", True, 7)
    over = await overrides_for(store, cid)
    assert all(is_enabled(s, over, cfg) for s in registry.tools.values() if s.skill == "notebook")


async def test_the_users_files_are_untouchable_and_a_mark_survives_a_restart(store, cfg):
    nb = Notebook(cfg)
    made = await nb.seed(store)
    assert {"identity.md", "opinions/README.md", "appearance/body.md"} <= set(made)
    (cfg.her.notebook / "appearance/body.md").unlink()
    assert await nb.seed(store) == [], "a placeholder it deleted stays deleted"
    c = ctx(store, cfg)
    assert (await nb.write(c, "identity.md", "I am nobody", "x")).is_error
    assert "I am nobody" not in (cfg.her.notebook / "identity.md").read_text()
    assert (await nb.delete(c, "opinions/README.md")).is_error
    assert (cfg.her.notebook / "opinions/README.md").exists()
    await nb.write(c, "opinions/tea.md", "meh", "tea")
    await nb.mark(store, "opinions/tea.md", True)
    assert (await nb.write(c, "opinions/tea.md", "x", "x")).is_error
    fresh = Notebook(cfg)
    await fresh.load_marks(store)
    assert fresh.owner("opinions/tea.md") == "marked"


async def test_an_overwritten_or_deleted_note_is_kept_for_the_user(store, cfg):
    nb = Notebook(cfg)
    c = ctx(store, cfg)
    await nb.write(c, "plans/a.md", "first", "a")
    await nb.write(c, "plans/a.md", "second", "a")
    assert "plans/a.md" in await build_index_fragment(store, cfg), "its index has it"
    old = list((cfg.her.notebook / ".versions/plans/a.md").iterdir())
    assert len(old) == 1 and "first" in old[0].read_text()
    assert not (await nb.delete(c, "plans/a.md")).is_error
    assert not (cfg.her.notebook / "plans/a.md").exists()
    assert [r["path"] for r in await notebook_index.list(store)] == []
    trashed = list((cfg.her.notebook / ".trash").glob("*/plans/a.md"))
    assert len(trashed) == 1 and "second" in trashed[0].read_text(), "the user keeps a copy"


def test_a_value_of_another_type_is_named_and_never_converted():
    from celine.turns.tools import mistyped
    schema = {"properties": {
        "find": {"type": "string"}, "part": {"type": "integer"},
        "wait": {"type": "boolean"},
        "before": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        "server_id": {"type": "string"}, "tab": {"type": ["string", "null"]}},
        "required": ["server_id"]}
    args = {"find": 192, "part": "2", "wait": "true", "before": 1111111111111111001,
            "server_id": 1111111111111111002, "tab": None}
    sent = dict(args)
    assert mistyped(args, schema) == ("find is a string; part is a whole number; wait is "
                                      "true or false; before is a string; server_id is a "
                                      "string")
    assert args == sent, "nothing converted"
    assert mistyped({"find": "x", "part": 2, "wait": True, "server_id": "1"}, schema) is None
    look = {"properties": {"what": {"type": "string", "enum": ["view", "locate"]}}}
    assert mistyped({"what": "find"}, look) == "what is one of view, locate"
