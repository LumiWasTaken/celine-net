"""The notebook's semantic index (skills/notebook_semantic.py) and its dense side in
recall (skills/recall.py) and notebook__search (skills/notebook.py): the canonical cut
(path, section, chunk), the model that owns the fingerprint, actual widths, incremental
builds, readiness, secondary failover, and the candidate rules that never let a dense hit
change eligibility or render new text under an old score."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from celine.boot import load
from celine.conversations import store as conversations
from celine.kb import embed as embed_mod
from celine.kb import semantic
from celine.skills import ToolContext
from celine.skills import notebook_index, notebook_semantic, recall
from celine.skills.notebook import Notebook

pytestmark = pytest.mark.anyio

WORDS = ("climb", "bakery", "tomato", "pizza")
NOISE = 0.001


def vvec(text: str, dim: int = 8) -> list[float]:
    v = [float(text.lower().count(w)) for w in WORDS]
    v += [NOISE] * max(0, dim - len(v))
    return v[:dim]


def FP(dim: int, shift: float = 0.0) -> tuple:
    return tuple(tuple(embed_mod.truncate([x + shift for x in vvec(p, dim)], dim))
                 for p in embed_mod.PROBES)


class Fake:
    """One Embeddings endpoint: its width and its fingerprint."""
    def __init__(self, fp, ref="fake", prefix="Q: ", batch=128, workers=2):
        self.fp = fp
        self.ref = ref
        self.s = {"batch": batch, "dims": len(fp[0]), "workers": workers,
                  "notebook_query_prefix": prefix, "query_prefix": "",
                  "max_input_chars": 12000}

    def embed(self, texts, **over):
        return [vvec(t, len(self.fp[0])) for t in texts]


def wire(monkeypatch, models: dict[str, tuple[float, int]], answering: list[str]):
    """Install a set of Embeddings endpoints: models maps ref -> (fingerprint shift,
    width); `answering` are the refs that answer now, in order. The first model of the
    dict is the configured primary."""
    def make(cfg, ref, timeout=None):
        shift, width = models[ref]
        fake = Fake(FP(width, shift), ref=ref)
        fake.s["batch"] = int(getattr(cfg, "embeddings", {}).get("batch", fake.s["batch"]))
        return fake

    monkeypatch.setattr(semantic, "embeddings_available", lambda cfg: True)
    monkeypatch.setattr(semantic, "Embedder", make)
    monkeypatch.setattr(semantic, "query_embedder", lambda cfg: make(cfg, answering[0]))
    monkeypatch.setattr(semantic, "_usable", lambda cfg: (
        SimpleNamespace(refs=list(answering), chain=list(models)), None))
    monkeypatch.setattr(semantic, "_embed",
                        lambda s, texts: [vvec(t, s["dims"]) for t in texts])


@pytest.fixture
def cfg():
    c = load()
    c.embeddings = {**c.embeddings, "dims": 8}
    c.settings.features = {**c.settings.features, "vector_notebook": True}
    return c


async def _note(store, cfg, path: str, text: str, summary: str = "a note") -> None:
    p = cfg.agent.notebook / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    await notebook_index.upsert(store, path, summary, len(text), text, cfg)


async def _build(cfg, store) -> int:
    return await notebook_semantic._pass(cfg, store, asyncio.get_running_loop())


async def _rows(store, sql: str, args: tuple = ()) -> list:
    return await store.read(lambda c: [dict(r) for r in c.execute(sql, args)])


async def _say(store, cfg, cid, text):
    from chat_fixtures import user_says
    mid, tid, _ = await user_says(store, cid, text)
    return await recall.block(cfg, store, cid, tid, {"id": mid, "content": text})


# ---- canonical cut ----------------------------------------------------------------------

def test_the_cut_splits_sections_and_repeated_headings_never_collide():
    body = "pre\n\n## A\none\n\n## A\ntwo\n\n## B\n\n## C\nthree\n"
    rows = notebook_index.cut(body, "p.md", "s")
    assert [(r["nth"], r["heading"]) for r in rows] == \
        [(0, ""), (1, "A"), (2, "A"), (3, "C")], "empty sections and the first heading's "\
        "body; repeated headings kept as distinct ordinals"
    assert rows[1]["sid"] != rows[2]["sid"], "repeated headings have distinct labels"
    assert rows[1]["chunk"] != rows[2]["chunk"] or rows[1]["nth"] != rows[2]["nth"]
    assert rows[0]["text"] == "pre"


def test_a_long_section_and_a_long_note_without_headings_lose_nothing():
    para = ("a line of roomy text that fills space " * 3).strip()
    long = "\n\n".join([para] * 25)
    rows = notebook_index.cut("## Big\n" + long, "p.md", "s")
    big = [r for r in rows if r["nth"] == 0]
    assert len(big) > 1, "a section over 2000 chars is split"
    assert len({r["sid"] for r in big}) == len(big), "chunks have distinct display labels"
    assert big[0]["text"].startswith(para.splitlines()[0])
    assert big[-1]["text"].endswith(long.rstrip()[-20:]), "the tail is not lost"
    # a headingless note gets the same fallback
    no_head = notebook_index.cut(long, "q.md", "s")
    assert len(no_head) > 1
    assert no_head[-1]["text"].endswith(long.rstrip()[-20:])
    joined = "\n".join(r["text"] for r in no_head)
    assert para.rstrip() in joined and para.rstrip() in joined


def test_a_pathological_body_terminates_and_never_yields_an_empty_chunk():
    body = "".join(f"para {i} " * (1 + i % 7) + "\n\n" for i in range(200))
    chunks = notebook_index._chunks(body)
    assert len(chunks) > 1 and all(c.strip() for c in chunks)
    assert chunks[-1].endswith(body.rstrip()[-15:]), "the tail is kept"
    # every paragraph's text appears somewhere
    for i in (0, 50, 199):
        assert f"para {i} " in "\n".join(chunks)


# ---- the scheduler ----------------------------------------------------------------------

async def test_the_feature_off_makes_no_embedding_request(store, cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(semantic, "_embed", lambda s, texts: calls.append(texts) or [])
    cfg.settings.features = {**cfg.settings.features, "vector_notebook": False}
    await _note(store, cfg, "diary/garden.md", "## Tomatoes\nripe\n")
    assert await _build(cfg, store) == 0
    assert calls == [] and not await _rows(store, "SELECT path FROM notebook_vecs")


async def test_an_unready_dense_index_does_not_get_a_query_embedding(
        store, cfg, monkeypatch):
    await _note(store, cfg, "people/noor.md", "keys: noor\n\nNoor climbs.")
    def forbidden(cfg, q, timeout=None):
        raise AssertionError("the dense query must wait for readiness")
    monkeypatch.setattr(notebook_semantic, "query_vector", forbidden)
    monkeypatch.setattr(recall, "_embed",
                        lambda cfg, query, texts: (vvec("climb"),
                                                   [vvec("climb") for _ in texts]))
    cid = await conversations.create(store)
    text, _ = await _say(store, cfg, cid, "noor")
    assert "Noor climbs." in text


async def test_the_width_is_taken_from_the_actual_vectors(store, cfg, monkeypatch):
    wire(monkeypatch, {"big": (0.0, 2560)}, ["big"])
    cfg.embeddings = {**cfg.embeddings, "dims": 4096}
    await _note(store, cfg, "diary/garden.md", "## Tomatoes\nripe tomatoes\n")
    assert await _build(cfg, store) == 1
    meta = await store.read(notebook_semantic.meta_of)
    assert meta["dims"] == 2560, "a configured 4096 with a 2560-dimensional model keeps 2560"
    rows = await _rows(store, "SELECT length(int8) AS n FROM notebook_vecs")
    assert rows and rows[0]["n"] == 2560


async def test_a_cancelled_pool_keeps_the_batches_already_committed(
        store, cfg, monkeypatch):
    wire(monkeypatch, {"fake": (0.0, 8)}, ["fake"])
    await _note(store, cfg, "diary/many.md",
                "".join(f"## H{i}\ntomato {i}\n\n" for i in range(5)))
    cfg.embeddings = {**cfg.embeddings, "batch": 2}
    cancelled = {"value": False}

    class Task:
        def cancelling(self):
            return cancelled["value"]

    monkeypatch.setattr(notebook_semantic.asyncio, "current_task", lambda: Task())
    save = notebook_semantic._save
    def save_and_cancel(c, batch, qs):
        save(c, batch, qs)
        cancelled["value"] = True
    monkeypatch.setattr(notebook_semantic, "_save", save_and_cancel)
    with pytest.raises(asyncio.CancelledError):
        await _build(cfg, store)
    assert len(await _rows(store, "SELECT path FROM notebook_vecs")) == 2, \
        "the first batch landed before cancellation, not only in the pool's memory"


async def test_more_than_400_sections_are_all_scored(store, cfg, monkeypatch):
    wire(monkeypatch, {"fake": (0.0, 8)}, ["fake"])
    body = "".join(f"## H{i}\ntomato {i}\n\n" for i in range(450))
    await _note(store, cfg, "diary/big.md", body)
    assert await _build(cfg, store) == 450
    qv = embed_mod.quantize(vvec("tomato", 8), 8, FP(8))
    rows = await store.read(lambda c: notebook_semantic.dense(c, qv))
    assert len(rows) == 450, "no arbitrary 400-row cutoff"
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True), "the best cosine score comes first"


async def test_an_unchanged_section_keeps_its_vector_across_an_edit(
        store, cfg, monkeypatch):
    wire(monkeypatch, {"fake": (0.0, 8)}, ["fake"])
    await _note(store, cfg, "diary/garden.md", "## Tomatoes\nripe\n\n## Work\nbakes\n")
    assert await _build(cfg, store) == 2
    before = {r["hash"]: r["int8"] for r in await _rows(
        store, "SELECT hash, int8 FROM notebook_vecs WHERE path='diary/garden.md'")}
    # edit only the second section, keeping the first's payload
    await _note(store, cfg, "diary/garden.md", "## Tomatoes\nripe\n\n## Work\nbakes bread\n")
    after = {r["hash"]: r["int8"] for r in await _rows(
        store, "SELECT hash, int8 FROM notebook_vecs WHERE path='diary/garden.md'")}
    assert set(before) & set(after), "the unchanged payload hash still has its vector"
    assert await _build(cfg, store) == 1, "only the changed chunk is embedded"


async def test_a_deleted_and_a_moved_note_reach_the_index(store, cfg, monkeypatch):
    wire(monkeypatch, {"fake": (0.0, 8)}, ["fake"])
    await _note(store, cfg, "diary/garden.md", "## Tomatoes\nripe\n")
    await _build(cfg, store)
    (cfg.agent.notebook / "notes").mkdir(exist_ok=True)
    (cfg.agent.notebook / "diary/garden.md").rename(cfg.agent.notebook / "notes/garden.md")
    assert await notebook_index.move(store, "diary/garden.md", "notes/garden.md") == 1
    assert not await _rows(store, "SELECT path FROM notebook_vecs"), \
        "the old path's payload and vector are gone"
    assert await _build(cfg, store) == 1, "the new path is embedded"
    assert await _rows(store, "SELECT path FROM notebook_vecs") == [{"path": "notes/garden.md"}]
    (cfg.agent.notebook / "notes/garden.md").unlink()
    await notebook_index.delete(store, "notes/garden.md")
    assert not await _rows(store, "SELECT path FROM notebook_sections")
    assert not await _rows(store, "SELECT path FROM notebook_vecs")


async def test_readiness_and_a_primary_model_change(store, cfg, monkeypatch):
    wire(monkeypatch, {"primary": (0.0, 8)}, ["primary"])
    await _note(store, cfg, "diary/garden.md", "## Tomatoes\nripe\n")
    assert not await store.read(notebook_semantic.ready_of)
    await _build(cfg, store)
    assert await store.read(notebook_semantic.ready_of)
    # the primary now gives other vectors: a rebuild, readiness false while it happens
    wire(monkeypatch, {"primary": (0.3, 8)}, ["primary"])
    assert await _build(cfg, store) == 1
    meta = await store.read(notebook_semantic.meta_of)
    assert embed_mod.matches(meta["probes"], FP(8, 0.3))
    assert await store.read(notebook_semantic.ready_of)


async def test_a_secondary_may_fail_over_when_it_matches_and_never_resets(
        store, cfg, monkeypatch):
    wire(monkeypatch, {"primary": (0.0, 8), "backup": (0.0, 8)}, ["primary"])
    await _note(store, cfg, "diary/garden.md", "## Tomatoes\nripe\n")
    await _build(cfg, store)
    meta = await store.read(notebook_semantic.meta_of)
    # the primary is down; the backup gives the same vectors: it takes over, no rebuild
    wire(monkeypatch, {"primary": (0.0, 8), "backup": (0.0, 8)}, ["backup"])
    assert await _build(cfg, store) == 0
    assert await store.read(notebook_semantic.meta_of) == meta
    assert await _rows(store, "SELECT path FROM notebook_vecs"), "vectors stay"
    # a backup with other vectors abstains; it must not reset
    wire(monkeypatch, {"primary": (0.0, 8), "backup": (0.6, 8)}, ["backup"])
    from celine.kb.importers import Unreachable
    with pytest.raises(Unreachable):
        await _build(cfg, store)
    assert await store.read(notebook_semantic.meta_of) == meta
    assert await _rows(store, "SELECT path FROM notebook_vecs")
    qv = embed_mod.quantize(vvec("tomato", 8), 8, FP(8, 0.6))
    assert not await store.read(lambda c: notebook_semantic.dense(c, qv)), \
        "a mismatching secondary abstains from the query too"


async def test_an_equal_length_external_edit_is_reindexed(store, cfg):
    await _note(store, cfg, "diary/garden.md", "## Tomatoes\nripe\n")
    before = (await _rows(store, "SELECT content FROM notebook_index"))[0]["content"]
    p = cfg.agent.notebook / "diary/garden.md"
    p.write_text("## Tomatoes\nRIpe\n")            # same length, other content
    fixed = await notebook_index.reconcile(store, cfg.agent.notebook, cfg)
    assert fixed["changed"] == 1
    after = (await _rows(store, "SELECT content FROM notebook_index"))[0]["content"]
    assert before != after


async def test_a_v1_hash_backfill_does_not_touch_the_note(store, cfg):
    await _note(store, cfg, "diary/garden.md", "## Tomatoes\nripe\n")
    await store.write(lambda c: c.execute(
        "UPDATE notebook_index SET content='', touched_at='old'"))
    fixed = await notebook_index.reconcile(store, cfg.agent.notebook, cfg)
    assert fixed == {"added": 0, "changed": 0, "removed": 0}
    row = (await _rows(store, "SELECT content, touched_at FROM notebook_index"))[0]
    assert row["content"] and row["touched_at"] == "old"


# ---- dense recall -----------------------------------------------------------------------

async def test_recall_finds_a_note_by_meaning_without_its_keys(store, cfg, monkeypatch):
    wire(monkeypatch, {"fake": (0.0, 8)}, ["fake"])
    await _note(store, cfg, "diary/garden.md",
                "## Tomatoes\nthe tomatoes are ripe this week, sweet ones\n\n## Work\nbakes\n")
    assert await _build(cfg, store) == 2
    cid = await conversations.create(store)
    text, report = await _say(store, cfg, cid, "how are the tomatoes")
    assert text.startswith(recall.HEAD + " ")
    assert "### diary/garden.md#Tomatoes (" in text and "ripe this week" in text
    assert "bakes" not in text
    states = {c["id"]: c["state"] for c in report["candidates"]}
    assert states["diary/garden.md#Tomatoes"] == recall.SHOWN
    assert all(c["key"] is None for c in report["candidates"])


async def test_unkeyed_dense_recall_uses_its_conservative_floor(store, cfg, monkeypatch):
    wire(monkeypatch, {"fake": (0.0, 8)}, ["fake"])
    await _note(store, cfg, "diary/garden.md", "## Tomatoes\nripe tomatoes")
    await _build(cfg, store)
    score = {"value": 0.6}
    real_dense = notebook_semantic.dense
    monkeypatch.setattr(notebook_semantic, "dense",
                        lambda c, qv, folder=None, k=None: [
                            {**r, "score": score["value"]}
                            for r in real_dense(c, qv, folder, k)])
    cid = await conversations.create(store)
    text, report = await _say(store, cfg, cid, "tomatoes")
    assert text == "" and report["candidates"][0]["state"] == recall.BELOW
    score["value"] = 0.7
    other = await conversations.create(store)
    text, report = await _say(store, cfg, other, "tomatoes")
    assert "ripe tomatoes" in text and report["dense_floor"] == 0.675


async def test_the_feature_probe_shows_index_progress_and_readiness(store, cfg, monkeypatch):
    wire(monkeypatch, {"fake": (0.0, 8)}, ["fake"])
    services = SimpleNamespace(store=store)
    await _note(store, cfg, "diary/garden.md", "## Tomatoes\nripe")
    pending = await notebook_semantic.probe(cfg, services)
    assert pending["detail"] == "Indexing notes: 0 of 1 section."
    await _build(cfg, store)
    ready = await notebook_semantic.probe(cfg, services)
    assert ready["detail"] == "Ready: 1 section indexed."


async def test_a_dense_hit_over_a_keyed_candidate_keeps_its_stored_score_and_is_not_embedded(
        store, cfg, monkeypatch):
    wire(monkeypatch, {"fake": (0.0, 8)}, ["fake"])
    await _note(store, cfg, "people/noor.md",
                "keys: noor\n\nNoor is a friend.\n\n## Climbing\nNoor climbs weekly.\n")
    await _build(cfg, store)
    embedded = []
    monkeypatch.setattr(notebook_semantic, "embed_texts",
                        lambda cfg, texts, timeout=None: embedded.append(texts) or [])
    calls = []
    real_qv = notebook_semantic.query_vector
    monkeypatch.setattr(notebook_semantic, "query_vector",
                        lambda cfg, q, timeout=None: calls.append(q) or real_qv(cfg, q))
    cid = await conversations.create(store)
    _, report = await _say(store, cfg, cid, "noor climbing")
    by_id = {c["id"]: c for c in report["candidates"]}
    assert by_id["people/noor.md#Climbing"]["source"] == "key+dense"
    assert by_id["people/noor.md#Climbing"]["score"] is not None
    assert calls == ["noor climbing"], "the query is embedded once"
    assert embedded == [], "the covered key candidate is not embedded again"


async def test_dense_candidates_never_push_keyed_candidates_past_the_cap(
        store, cfg, monkeypatch):
    wire(monkeypatch, {"fake": (0.0, 8)}, ["fake"])
    for i in range(41):
        await _note(store, cfg, f"people/n{i}.md", f"keys: noor\n\nNoor fact {i}.")
    await _note(store, cfg, "diary/garden.md", "## Tomatoes\ntomato tomato tomato")
    await _build(cfg, store)
    cid = await conversations.create(store)
    _, report = await _say(store, cfg, cid, "noor tomato")
    assert len(report["candidates"]) == recall.CANDIDATES
    assert all(c["source"] != "dense" for c in report["candidates"]), \
        "dense-only candidates get only the slots left by the keyed candidates"


async def test_one_query_call_when_no_fresh_key_section_is_needed(store, cfg, monkeypatch):
    wire(monkeypatch, {"fake": (0.0, 8)}, ["fake"])
    await _note(store, cfg, "people/noor.md", "keys: noor\n\nNoor is a friend.\n")
    await _build(cfg, store)
    calls, embedded = [], []
    real_qv = notebook_semantic.query_vector
    monkeypatch.setattr(notebook_semantic, "query_vector",
                        lambda cfg, q, timeout=None: calls.append(q) or real_qv(cfg, q))
    monkeypatch.setattr(notebook_semantic, "embed_texts",
                        lambda cfg, texts, timeout=None: embedded.append(texts) or [])
    cid = await conversations.create(store)
    await _say(store, cfg, cid, "noor")
    assert len(calls) == 1 and embedded == []


async def test_stale_hydration_never_renders_changed_text_under_an_old_score(store, cfg):
    await _note(store, cfg, "diary/garden.md", "## Tomatoes\nripe\n")
    row = notebook_index.cut("## Tomatoes\nripe\n", "diary/garden.md", "a note")[0]
    x = {"path": "diary/garden.md", "nth": row["nth"], "chunk": row["chunk"],
         "hash": row["hash"], "summary": "a note"}
    assert recall._hydrate(cfg.agent.notebook, cfg, x, {})
    assert x["text"] == "ripe"
    await _note(store, cfg, "diary/garden.md", "## Tomatoes\nSOMETHING else\n")
    stale = {"path": "diary/garden.md", "nth": row["nth"], "chunk": row["chunk"],
             "hash": row["hash"], "summary": "a note"}
    assert not recall._hydrate(cfg.agent.notebook, cfg, stale, {}), \
        "the old hash does not match: nothing is rendered"
    assert stale.get("text") is None


# ---- hybrid search ----------------------------------------------------------------------

async def test_search_fuses_word_hits_with_meaning_hits_and_shows_an_excerpt(
        store, cfg, monkeypatch):
    wire(monkeypatch, {"fake": (0.0, 8)}, ["fake"])
    nb = Notebook(cfg)
    cid = await conversations.create(store)
    ctx = ToolContext(cid, "t", "x", store, cfg, None)  # type: ignore[arg-type]
    await nb.write(ctx, "opinions/food.md", "- pizza: love it\n", "food")
    await _note(store, cfg, "diary/garden.md",
                "## Tomatoes\ntomato plants need water\n\n## Work\nbakes bread\n")
    await _build(cfg, store)
    out = await nb.search(ctx, "pizza", 10)
    assert "opinions/food.md" in out.content, "the word search finds the pizza note"
    assert "diary/garden.md" in out.content and "tomato plants need water" in out.content, \
        "the meaning-only hit carries a real excerpt, not an empty body"
    assert "  ## Tomatoes" in out.content
    # a manual search still finds a load: off note
    await nb.write(ctx, "lore/hidden.md", "keys: x\nload: off\n\ntomato secret\n", "hidden")
    await _build(cfg, store)
    out = await nb.search(ctx, "pizza", 10)
    assert "lore/hidden.md" in out.content


async def test_search_honours_its_limit_with_the_feature_off(store, cfg):
    cfg.settings.features = {**cfg.settings.features, "vector_notebook": False}
    nb = Notebook(cfg)
    cid = await conversations.create(store)
    ctx = ToolContext(cid, "t", "x", store, cfg, None)  # type: ignore[arg-type]
    for i in range(5):
        await nb.write(ctx, f"opinions/food{i}.md", "- pizza: love it\n", "food")
    out = await nb.search(ctx, "pizza", 2)
    assert out.display["hits"] == 2


async def test_dense_folder_filtering_happens_before_the_limit(store, cfg, monkeypatch):
    wire(monkeypatch, {"fake": (0.0, 8)}, ["fake"])
    for i in range(6):
        await _note(store, cfg, f"agents/other/n{i}.md", "tomato tomato")
    await _note(store, cfg, "agents/scout/garden.md", "tomato")
    await _build(cfg, store)
    parent = await conversations.create(store)
    sub = await conversations.create(store, agent_of=parent, agent_name="scout",
                                     notebook_folder="agents/scout")
    nb = Notebook(cfg)
    ctx = ToolContext(sub, "t", "x", store, cfg, None)  # type: ignore[arg-type]
    out = await nb.search(ctx, "tomato", 1)
    assert "agents/scout/garden.md" in out.content
    assert "agents/other" not in out.content


def test_stored_texts_are_embedded_behind_the_text_prefix(monkeypatch):
    from celine.kb import semantic
    seen = []
    monkeypatch.setattr(semantic, "_embed", lambda s, texts: seen.extend(texts) or [[0.0]] * len(texts))
    semantic._embed_texts({"text_prefix": "passage: "}, ["a", "b"])
    assert seen == ["passage: a", "passage: b"]
