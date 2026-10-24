"""A notebook on disk and in the index, a config around it, and a turn's selection of
what it carries by need (turns/loads.py), for the loads and note picker tests."""
from __future__ import annotations

from types import SimpleNamespace

from chat_fixtures import user_says

from celine.skills import notebook_index
from celine.turns import loads

WINDOW = 10_000         # notes 1,000 tokens, tools 800
BACKEND = SimpleNamespace(cfg=SimpleNamespace(raw={"context": WINDOW}, base_url="test",
                                              params={}), model="loads")
TEXTS: dict[str, str] = {}        # what write_notes wrote, by path


def notes_cfg(tmp_path, tools=None):
    return SimpleNamespace(
        her=SimpleNamespace(notebook=tmp_path / "notebook"),
        user="Joe", context=dict(loads.DEFAULTS),
        substitute=lambda t: t.replace("{{username}}", "Joe"), skill=lambda _: {}, skill_enabled=lambda _: True,
        settings=SimpleNamespace(tools=tools or {}))


async def write_notes(store, cfg, files: dict[str, str]) -> None:
    for path, text in files.items():
        p = cfg.her.notebook / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        TEXTS[path] = text
        await notebook_index.upsert(store, path, "a note", len(text), text)


async def select_turn(store, cfg, reg, cid, n: int, text: str = "fine"):
    """A user message, the selection of the turn it starts, and its short answer."""
    from celine.conversations import tree
    mid, tid, _ = await user_says(store, cid, text)
    got = await loads.select(store, cfg, reg, cid, BACKEND, when="start", turn_id=tid)
    reply = await tree.start_assistant_message(store, cid, mid, tid)
    await tree.finish_assistant_message(store, cid, reply, "ok", None)
    await store.write(lambda c: c.execute("UPDATE turns SET state='done'"))
    return got
