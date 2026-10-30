"""The notebook as its space: no path leaves it, a sub-agent stays in its folder,
the user's notes and tools/ are not its own to change, hashline edits refuse stale
anchors, a write says where it saved, web citations are taken out, a note it only read
ages out after one turn."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from celine.conversations import store as conversations
from celine.boot import load
from celine.skills import ToolContext
from celine.skills import notebook as notebook_mod
from celine.skills.notebook import Notebook
from celine.skills import notebook_index

pytestmark = pytest.mark.anyio

STAMP = "Last changed: 2026-09-24 21:52"     # the fixed clock below, in Europe/Berlin
NOTE = "# food\n- pizza: love it\n- olives: gross\n\n## drinks\n- coffee: yes\n"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(notebook_mod, "_now",
                        lambda: datetime(2026, 9, 24, 19, 52, tzinfo=timezone.utc))


@pytest.fixture
def cfg():
    c = load()
    c.timezone = "Europe/Berlin"
    return c


def _ctx(cid, store, cfg):
    return ToolContext(cid, "t", "x", store, cfg, None)  # type: ignore[arg-type]


def _anchors(out: str) -> dict[int, str]:
    """line number -> `n:hash`, from what notebook__read or notebook__edit showed."""
    return {int(m[1]): f"{m[1]}:{m[2]}"
            for m in re.finditer(r"^(\d+):([0-9a-z]{2})\| ", out, re.M)}


async def _note(nb, ctx, path="opinions/food.md"):
    await nb.write(ctx, path, NOTE, "food")
    return _anchors((await nb.read(ctx, path)).content)


async def test_a_limited_conversation_stays_inside_its_folder(store, cfg):
    nb = Notebook(cfg)
    await nb.seed(store)
    whole = _ctx("c", store, cfg)
    await nb.write(whole, "plans/secret.md", "the big plan", "the plan")
    a = _anchors((await nb.read(whole, "plans/secret.md")).content)
    parent = await conversations.create(store)
    sub = await conversations.create(store, agent_of=parent, agent_name="scout one",
                                          notebook_folder="agents/scout")
    ctx = _ctx(sub, store, cfg)

    assert not (await nb.write(ctx, "agents/scout/new.md", "found it", "finds")).is_error
    for call in (lambda p: nb.read(ctx, p), lambda p: nb.delete(ctx, p),
                 lambda p: nb.list(ctx, p),
                 lambda p: nb.write(ctx, p, "overwritten", "x"),
                 lambda p: nb.edit(ctx, p, [{"op": "delete", "start": a[2]}])):
        for bad in ("plans/secret.md", "agents/other.md", "agents/scout/../../x.md"):
            out = await call(bad)
            assert out.is_error and out.content == (
                f"{bad} is outside your folder agents/scout."), bad
    out = await nb.move(ctx, "agents/scout/new.md", "x.md")
    assert out.is_error and out.content == "x.md is outside your folder agents/scout."
    assert (cfg.her.notebook / "plans/secret.md").read_text() == f"{STAMP}\nthe big plan"
    assert "plans/" not in (await nb.list(ctx, None)).content
    assert "secret" not in (await nb.search(ctx, "plan")).content
    assert "plans/secret.md" not in await nb.prompt_fragment(ctx)
    # the parent still sees everything
    assert "plans/secret.md" in (await nb.list(_ctx(parent, store, cfg), None)).content


async def test_her_notes_move_and_the_users_and_tools_stay(store, cfg):
    nb = Notebook(cfg)
    await nb.seed(store)
    ctx = _ctx("c", store, cfg)
    await nb.write(ctx, "./scratch/cars.md", "red ones", "cars")
    await nb.write(ctx, "scratch//cars.md", "blue ones", "cars again")
    assert [r["path"] for r in await notebook_index.list(store, None)
            if "cars" in r["path"]] == ["scratch/cars.md"]
    assert not (await nb.move(ctx, "scratch", "sorted/")).is_error
    assert (cfg.her.notebook / "sorted/cars.md").read_text() == f"{STAMP}\nblue ones"
    assert list((cfg.her.notebook / ".versions/sorted/cars.md").iterdir()), \
        "the history follows the move"
    assert "sorted/cars.md" in (await nb.search(ctx, "blue")).content
    out = await nb.move(ctx, "sorted/cars.md", "../cars.md")
    assert out.is_error and out.content == "../cars.md is outside your notebook."

    ident = (cfg.her.notebook / "identity.md").read_text()
    top = _anchors((await nb.read(ctx, "identity.md")).content)[1]
    await nb.mark(store, "sorted/cars.md", True)        # the user's now
    for out in (await nb.write(ctx, "identity.md", "mine now", "x"),
                await nb.edit(ctx, "identity.md", [{"op": "delete", "start": top}]),
                await nb.delete(ctx, "identity.md"),
                await nb.move(ctx, "identity.md", "me.md"),
                await nb.write(ctx, "sorted/cars.md", "mine", "x"),
                await nb.write(ctx, "tools/x.md", "x", "x"),
                await nb.read(ctx, "tools/archive.md"),
                await nb.delete(ctx, "tools/archive.md")):
        assert out.is_error, out.content
    assert (cfg.her.notebook / "identity.md").read_text() == ident
    assert (cfg.her.notebook / "sorted/cars.md").read_text() == f"{STAMP}\nblue ones"
    assert not (cfg.her.notebook / "tools").exists()


async def test_a_stale_anchor_is_refused_and_fresh_ones_apply(store, cfg):
    nb = Notebook(cfg)
    ctx = _ctx("c", store, cfg)
    a = await _note(nb, ctx)
    await nb.edit(ctx, "opinions/food.md", [{"op": "insert", "after": "start",
                                             "text": "updated today"}])
    out = await nb.edit(ctx, "opinions/food.md",
                        [{"op": "replace", "start": a[2], "text": "# FOOD"},
                         {"op": "replace", "start": a[4], "text": "- olives: fine"}])
    assert out.is_error
    p = cfg.her.notebook / "opinions/food.md"
    assert p.read_text() == f"{STAMP}\nupdated today\n{NOTE}", "nothing changed"
    fresh = _anchors((await nb.read(ctx, "opinions/food.md")).content)
    out = await nb.edit(ctx, "opinions/food.md", [
        {"op": "replace", "start": fresh[5], "text": "- olives: fine"},
        {"op": "insert", "after": fresh[4], "text": "- pineapple: fine"}])
    assert not out.is_error
    assert p.read_text() == (f"{STAMP}\nupdated today\n# food\n- pizza: love it\n"
                             "- pineapple: fine\n- olives: fine\n\n## drinks\n- coffee: yes\n")


async def test_the_appearance_is_seeded_only_with_images_on(store, cfg):
    nb = Notebook(cfg)
    cfg.settings.features = {**cfg.settings.features, "images": False}
    made = await nb.seed(store)
    assert "identity.md" in made and "curious.md" in made
    assert not any(p.startswith("appearance/") for p in made)
    assert not (cfg.her.notebook / "appearance").exists()
    cfg.settings.features = {**cfg.settings.features, "images": True}
    assert sorted(await nb.seed(store)) == [
        "appearance/README.md", "appearance/body.md", "appearance/clothes.md",
        "appearance/face.md", "appearance/hair.md"], "turned on later: made at the next start"
    (cfg.her.notebook / "appearance/body.md").unlink()
    assert await nb.seed(store) == [], "a placeholder it deleted stays deleted"


async def test_the_seed_waits_for_the_names(store, cfg):
    nb = Notebook(cfg)
    cfg.user = "User"                                   # the default, before the first run
    made = await nb.seed(store)
    assert "appearance/README.md" in made and "appearance/body.md" in made
    assert not any(p.startswith("voice/") for p in made), "how it talks is its talk"
    assert not any(p.startswith("opinions/User") for p in made)
    (cfg.her.notebook / "appearance/body.md").unlink()
    cfg.user = "Joe"                                    # named on the first run
    assert await nb.seed(store) == ["opinions/Joe.md"], "only the named one, nothing it deleted"
    assert "what you think of Joe" in (cfg.her.notebook / "opinions/Joe.md").read_text()
    assert await nb.seed(store) == []
    ident = (cfg.her.notebook / "identity.md").read_text()
    assert "swear" not in ident and "crude" not in ident
    assert "you are {{name}}." in ident
    cfg.her.name = "Mira"           # named on the first run, after the seed
    assert "you are Mira." in nb.identity()
    assert "you are Mira." in (await nb.read(_ctx("c", store, cfg), "identity.md")).content


async def test_a_dotdot_path_is_checked_where_it_lands(store, cfg):
    nb = Notebook(cfg)
    await nb.seed(store)
    ctx = _ctx("c", store, cfg)
    ident = (cfg.her.notebook / "identity.md").read_text()
    assert (await nb.write(ctx, "opinions/../identity.md", "mine now", "x")).is_error
    assert (await nb.delete(ctx, "opinions/../identity.md")).is_error
    assert (cfg.her.notebook / "identity.md").read_text() == ident
    await nb.write(ctx, "opinions/../plans/a.md", "a plan", "plan")
    paths = [r["path"] for r in await notebook_index.list(store, None)]
    assert "plans/a.md" in paths and not any(".." in p for p in paths)


async def test_a_folder_or_a_missing_note_is_said_plainly_with_near_names(store, cfg):
    nb = Notebook(cfg)
    await nb.seed(store)
    ctx = _ctx("c", store, cfg)
    for out in (await nb.read(ctx, "opinions"), await nb.delete(ctx, "opinions"),
                await nb.delete(ctx, "nope.md")):
        assert out.is_error and "notebook__list" in out.content, out.content
    assert (cfg.her.notebook / "opinions/README.md").exists()
    # a missing note points at the names near it
    await nb.write(ctx, "radar/2026-10-21.md", "x", "x")
    await nb.write(ctx, "radar/2026-10-19.md", "y", "y")
    out = await nb.read(ctx, "radar/2026-10-20.md")
    assert out.is_error
    assert "radar/2026-10-21.md" in out.content and "radar/2026-10-19.md" in out.content


async def test_search_takes_plain_words_with_punctuation(store, cfg):
    nb = Notebook(cfg)
    await nb.seed(store)
    ctx = _ctx("c", store, cfg)
    await nb.write(ctx, "people/noor.md", "noor's birthday is in may, send an e-mail", "noor")
    for q in ("noor's birthday", "e-mail", "in may?", "birthday."):
        assert "people/noor.md" in (await nb.search(ctx, q)).content, q


async def test_a_search_shows_each_matching_section_with_its_lines_to_read_from(store, cfg):
    nb = Notebook(cfg)
    await nb.seed(store)
    ctx = _ctx("c", store, cfg)
    body = "\n".join([
        "keys: noor, climbing", "", "Noor, a friend from the gym.",
        "## Weekends", "Saturdays are long.", "Noor climbs every weekend at the hall.",
        "She wants outdoor routes.",
        "## Food", "Sushi on Fridays.",
        "## Trips", "A climbing trip to Arco in May.", "", "Tents and ropes.",
        "## Gear", "Her shoes are worn from climbing.",
        "## Plans", "Climbing in Fontainebleau next spring."])
    await nb.write(ctx, "people/noor.md", body, "Noor, a climbing friend")
    out = (await nb.search(ctx, "climbing")).content
    head, *found = out.splitlines()
    assert re.fullmatch(r"people/noor\.md \(\d+ \w+\) - Noor, a climbing friend "
                        r"\[keys: noor, climbing\]", head), head
    assert found == [
        "  ## Weekends",
        "  6- Saturdays are long.",
        "  7: Noor climbs every weekend at the hall.",
        "  8- She wants outdoor routes.",
        "  ## Trips",
        "  12: A climbing trip to Arco in May.",
        "  ## Gear",
        "  16: Her shoes are worn from climbing.",
        "  also in: Plans",
    ], "per section its heading and its match between its neighbours, numbered as "\
       "notebook__read numbers them; the stemmed word found too; the head lines not"
    read = (await nb.read(ctx, "people/noor.md", from_line=7)).content
    assert read.splitlines()[0].startswith("7:") and "Noor climbs" in read.splitlines()[0]


async def test_the_stamp_has_no_anchor_and_text_as_a_list_is_joined(store, cfg):
    """The agent's real edits: 27 failed trying to edit line 1 (the stamp), and a text sent as a
    list of lines crashed. Line 1 shows plain, and a list joins instead of crashing."""
    nb = Notebook(cfg)
    ctx = _ctx("c", store, cfg)
    a = await _note(nb, ctx)
    read = (await nb.read(ctx, "opinions/food.md")).content
    assert read.splitlines()[0] == STAMP, "the stamp comes plain, no n:hash"
    assert 1 not in _anchors(read), "line 1 has no anchor to copy"
    out = await nb.edit(ctx, "opinions/food.md",
                        [{"op": "insert", "after": a[2], "text": ["- apples", "- pears"]}])
    assert not out.is_error, out.content
    body = (cfg.her.notebook / "opinions/food.md").read_text()
    assert "- apples\n- pears" in body and body.splitlines()[0] == STAMP


async def test_a_write_says_where_it_saved_and_the_list_has_dates(store, cfg):
    nb = Notebook(cfg)
    cid = await conversations.create(store)
    ctx = ToolContext(cid, "t", "x", store, cfg, None)  # type: ignore[arg-type]
    out = await nb.write(ctx, "diary/x.md", "a day", "my day")
    assert out.content == "Saved diary/x.md."
    await store.write(lambda c: c.execute(
        "UPDATE notebook_index SET touched_at='2026-10-16T05:00:00.000+00:00' "
        "WHERE path='diary/x.md'"))
    listed = (await nb.list(ctx)).content
    assert "diary/x.md (16 Oct) - my day" in listed


async def test_a_web_citation_she_writes_down_is_stored_as_written(store, cfg):
    nb = Notebook(cfg)
    cid = await conversations.create(store)
    ctx = ToolContext(cid, "t", "x", store, cfg, None)  # type: ignore[arg-type]
    await nb.write(ctx, "research/mail.md", "prices [[web:3]] and [web:9]【2†source】.\n"
                   "download: https://example.com/a.zip", "mail")
    text = (cfg.her.notebook / "research/mail.md").read_text()
    assert "prices [[web:3]] and [web:9]【2†source】.\ndownload: https://example.com/a.zip" in text


async def test_the_apps_own_files_are_not_notes(store, cfg):
    """A refusal is the tool's answer, never a crash: no tool raises on a path."""
    nb = Notebook(cfg)
    await nb.seed(store)
    ctx = _ctx("c", store, cfg)
    for bad in (".index.db", ".versions/a.md", "x/.hidden.md"):
        for out in (await nb.write(ctx, bad, "x", "x"), await nb.read(ctx, bad),
                    await nb.edit(ctx, bad, [{"op": "delete", "start": "1:aa"}]),
                    await nb.delete(ctx, bad), await nb.move(ctx, bad, "y.md"),
                    await nb.move(ctx, "identity.md", bad)):
            assert out.is_error and out.content == (
                f"{bad} is not in your notebook: notebook__list shows your notes."), bad


async def test_index_paths_match_exactly(store):
    for p in ("a_b/x.md", "aXb/x.md", "A_B/x.md", "a%b/x.md", "azb/x.md"):
        await notebook_index.upsert(store, p, "s", 1)
    assert [r["path"] for r in await notebook_index.list(store, "a_b")] == ["a_b/x.md"]
    assert await notebook_index.move(store, "a_b", "moved") == 1
    assert await notebook_index.delete_prefix(store, "a%b") == 1
    left = [r["path"] for r in await notebook_index.list(store)]
    assert left == ["A_B/x.md", "aXb/x.md", "azb/x.md", "moved/x.md"]
