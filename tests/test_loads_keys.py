"""Words in the talk (turns/loads.py): a note's `keys:` line, a whole word or phrase in
any case, read again when the note changes, no longer loading the note in full (its
sections come back graded, skills/recall.py); and a routine's {{on_mind}}."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from notes_fixtures import notes_cfg, select_turn, write_notes

from celine.conversations import store as conversations
from celine.skills import notebook
from celine.turns import loads

pytestmark = pytest.mark.anyio

NOTES = {
    "people/noor.md": "keys: Noor, climbing\n\nNoor climbs every weekend.",
    "opinions/food.md": "keys: art, pineapple pizza\n\nPineapple pizza is great.",
    "lore/tower.md": "keys: lighthouse\nload: off\n\nThe lighthouse.",
    "lore/plain.md": "No keys here. Noor is named in the text only.",
}


def _why(got, path):
    return next(n["why"] for n in got.notes if n["path"] == path)


def test_a_key_is_a_whole_word_or_phrase():
    assert loads.key_in("the artist", ["art"]) is None
    assert loads.key_in("art.", ["art"]) == "art"


async def test_a_key_no_longer_loads_a_note_in_full(store, tmp_path):
    """Its sections come back graded under [your memory] instead (test_memory_recall)."""
    cfg = notes_cfg(tmp_path)
    await write_notes(store, cfg, NOTES)
    cid = await conversations.create(store)
    got = await select_turn(store, cfg, None, cid, 1, "how is NOOR doing?")
    assert got.loaded_notes == frozenset()


async def test_the_keys_are_read_again_when_the_note_changes(store, tmp_path):
    cfg = notes_cfg(tmp_path)
    await write_notes(store, cfg, {"people/noor.md": "keys: noor\n\nx"})
    root = cfg.her.notebook
    assert notebook.keys_of(root, "people/noor.md") == ["noor"]
    p = root / "people/noor.md"
    p.write_text("keys: noor, mountains\n\nx")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert notebook.keys_of(root, "people/noor.md") == ["noor", "mountains"]
    assert notebook.keys_of(root, "people/gone.md") == []


async def test_a_routines_on_mind_names_the_notes_her_last_thoughts_key(store, tmp_path):
    from celine.schedules.variables import NONE, on_mind
    cfg = notes_cfg(tmp_path)
    await write_notes(store, cfg, NOTES)
    app = SimpleNamespace(cfg=cfg, store=store)
    assert await on_mind(app) == NONE
    (cfg.her.notebook / "thoughts").mkdir()
    (cfg.her.notebook / "thoughts" / "2026-10-16.md").write_text(
        "Last changed: x\n- 10:00 noor went climbing again\n")
    assert await on_mind(app) == "- people/noor.md: a note"
    many = {f"people/p{i}.md": "keys: noor\n\nx" for i in range(10)}
    await write_notes(store, cfg, many)
    assert len((await on_mind(app)).splitlines()) == 8, "the most recently touched 8"
