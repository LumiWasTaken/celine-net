"""Images and music against a fake ComfyUI: a batch under one GPU lease (one unload, one
free at its end, before the lease is released), a failed item that fails only its slot,
a Stop, its call that returns at once and fills in place without a job or waking it,
or waits with wait=true, which images an edit may start from, the routes, the
preset files, and the user's presets (an upload with placeholders, an edit of a shipped
one and its reset). Fake servers only."""
from __future__ import annotations

from chat_fixtures import Scripted, until, user_says

import contextlib
import json
from datetime import datetime, timezone

import anyio
import httpx
import pytest

from celine.backends.llm import LLM
from celine.backends.router import Router
from celine.boot import load
from celine.conversations import store as conversations, tree
from celine.core.bus import EventBus
from celine.core.services import Services
from celine.comfyui import api as comfy_api, batches as batches_mod, presets, server
from celine.image_catalog import Catalog, Resource
from celine.jobs.jobs import Jobs
from celine.skills import Registry, ToolContext, build as build_registry
from celine.skills.images import ImageGenerator
from celine.skills.music import MusicGenerator
from celine.turns import store as turns
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from celine import attachments
from settings_fixtures import client, scratch

pytestmark = pytest.mark.anyio

PROVIDERS = {
    "swap": {"kind": "llama-swap", "label": "llama-swap", "base_url": "http://swap:1/v1",
             "models": {"big": {"caps": ["chat", "tools"]}}},
    "comfy": {"kind": "comfyui", "label": "ComfyUI", "base_url": "http://comfy:8188",
              "options": {"shares_gpu_with": "swap"},
              "models": {"anima-turbo": {"label": "Anima turbo"},
                         "krea2-turbo": {"label": "Krea 2 turbo"},
                         "krea2-identity-edit": {"label": "Krea 2 identity edit"},
                         "moss-image-edit": {"label": "MOSS-Image edit"},
                         "yue2": {"label": "YuE2"}}},
}
ROLES = {"chat": {"chain": ["swap/big"]},
         "image_generation": {"chain": ["comfy/anima-turbo", "comfy/krea2-turbo",
                                        "comfy/krea2-identity-edit",
                                        "comfy/moss-image-edit"]},
         "music_generation": {"chain": ["comfy/yue2"]}}


def png(w: int, h: int) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + b"x" * 64)


class Fake:
    """A ComfyUI at comfy and a llama-swap at swap. Prompts are p1, p2, ..."""

    def __init__(self):
        self.calls: list[str] = []
        self.bodies: dict[str, list] = {}
        self.graphs: list[dict] = []
        self.hang = False
        self.fail: set[str] = set()
        self.lacks: dict[str, str] = {}     # node class -> an input whose file is not there
        self.dead = False                   # ComfyUI answers its probe, then nothing else
        self.lm = 404                       # LoRA-Manager's answer: 404 not installed
        self.llm: LLM | None = None
        self.lease_at_free: bool | None = None
        self.queued = anyio.Event()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path, method = request.url.host, request.url.path, request.method
        self.calls.append(f"{host} {method} {path}")
        if request.headers.get("content-type") == "application/json":
            self.bodies.setdefault(f"{host} {path}", []).append(json.loads(request.content))
        if host == "comfy" and self.dead and path != "/system_stats":
            raise httpx.ConnectError("refused", request=request)
        if host == "swap":
            if path == "/running":
                return httpx.Response(200, json={"running": []})
            return httpx.Response(200, json={"msg": "ok", "data": []})
        if path.startswith("/api/lm/"):
            return httpx.Response(self.lm, text="oops")
        if path.startswith("/object_info/"):
            cls = path.rsplit("/", 1)[1]
            spec = {"input": {"required": {}}}
            if cls in self.lacks:            # a combo whose list lacks the preset's file
                spec["input"]["required"][self.lacks[cls]] = [["something-else.safetensors"]]
            return httpx.Response(200, json={cls: spec})
        if path == "/upload/image":
            return httpx.Response(200, json={"name": "celine-src.png", "subfolder": "",
                                             "type": "input"})
        if path == "/prompt":
            self.graphs.append(json.loads(request.content)["prompt"])
            self.queued.set()
            return httpx.Response(200, json={"prompt_id": f"p{len(self.graphs)}"})
        if path == "/queue" and method == "GET":
            return httpx.Response(200, json={"queue_running": [[1, f"p{len(self.graphs)}"]],
                                             "queue_pending": []})
        if path.startswith("/history/"):
            pid = path.rsplit("/", 1)[1]
            if self.hang:
                return httpx.Response(200, json={})
            if pid in self.fail:
                return httpx.Response(200, json={pid: {"status": {
                    "status_str": "error", "completed": False, "messages": [
                        ["execution_error", {"node_type": "KSampler",
                                             "exception_message": "CUDA out of memory."}]]},
                    "outputs": {}}})
            out = {"audio": [{"filename": "celine_1.mp3", "type": "output"}]} \
                if "10" in self.graphs[-1] and "style" in self.graphs[-1]["10"]["inputs"] \
                else {"images": [{"filename": "ComfyUI_temp_1.png", "type": "temp"}]}
            outputs = {"9": out}
            return httpx.Response(200, json={pid: {
                "status": {"status_str": "success", "completed": True, "messages": []},
                "outputs": outputs}})
        if path == "/view":
            if request.url.params["filename"].endswith(".mp3"):
                return httpx.Response(200, content=b"ID3" + b"x" * 64,
                                      headers={"content-type": "audio/mpeg"})
            return httpx.Response(200, content=png(832, 1216),
                                  headers={"content-type": "image/png"})
        if path == "/free":
            self.lease_at_free = bool(self.llm and self.llm.scheduler.running)
        return httpx.Response(200)

    def count(self, what: str) -> int:
        return self.calls.count(what)


@pytest.fixture
async def app(tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles=ROLES)
    monkeypatch.setattr(server, "POLL", 0.01)
    cfg = load()
    fake = Fake()
    llm = LLM(cfg, router=Router(cfg, transport=httpx.MockTransport(fake)))
    fake.llm = llm
    async with anyio.create_task_group() as tg:
        services = Services(cfg=cfg, store=store, llm=llm, bus=EventBus())
        services.batches = batches_mod.Batches(services, tg)
        services.fake = fake
        yield services
        tg.cancel_scope.cancel()


async def _call(store, cid, call_id, tool="image_generator__generate"):
    """A tool call row in a reply of `cid`, as a turn records it."""
    _, tid, _ = await user_says(store, cid, "go")
    mid = await tree.start_assistant_message(store, cid, None, tid)
    await turns.record_tool_calls(store, cid, mid, [{"id": call_id, "name": tool,
                                                     "arguments": "{}"}])

    async def emit(line, **data):
        pass
    return ToolContext(cid, tid, call_id, store, None, emit), mid


# ---- a batch -------------------------------------------------------------------------

async def test_a_batch_runs_under_one_lease_and_frees_once_at_its_end(app, store):
    fake = app.fake
    b = await app.batches.create("images", [{"prompt": "1girl, solo\nShe reads.",
                                             "seed": 7}], count=2)
    assert [i["state"] for i in b["items"]] == ["waiting", "waiting"]
    assert [i["seed"] for i in b["items"]] == [7, 8], "count: one seed each"
    b = await app.batches.run(b["id"])
    assert b["state"] == "done" and [i["state"] for i in b["items"]] == ["done", "done"]
    assert fake.count("swap POST /api/models/unload") == 1
    assert fake.count("comfy POST /prompt") == 2 and fake.count("comfy POST /free") == 1
    assert fake.calls.index("comfy POST /free") > fake.calls.index("comfy POST /prompt")
    assert fake.lease_at_free is True, "ComfyUI frees its memory before the lease ends"
    assert app.llm.scheduler.running == [] and app.llm.scheduler.waiting == []
    assert fake.graphs[0]["4"]["inputs"]["text"] == "1girl, solo\nShe reads."
    assert fake.graphs[1]["7"]["inputs"]["seed"] == 8
    att = b["items"][0]["attachment"]
    assert (att["width"], att["height"], att["media_type"]) == (832, 1216, "image/png")
    assert (await attachments.get(store, att["id"])) is not None
    assert list((app.cfg.data_dir / "images").rglob("*.png")), "kept as a file too"
    assert (await batches_mod.get(store, b["id"]))["items"] == b["items"]


async def test_a_failed_item_fails_its_slot_and_the_rest_are_made(app):
    fake = app.fake
    fake.fail = {"p1"}
    b = await app.batches.create("images", [
        {"prompt": "a"}, {"prompt": "b", "preset": "nope"}, {"prompt": "c"},
        {"prompt": "d", "width": 9000, "height": 9000}])
    assert [i["state"] for i in b["items"]] == ["waiting", "failed", "waiting", "failed"]
    assert "no preset 'nope' for new images" in b["items"][1]["error"]
    assert "9000x9000 is outside the limits" in b["items"][3]["error"], "failed, not scaled"
    b = await app.batches.run(b["id"])
    assert [i["state"] for i in b["items"]] == ["failed", "failed", "done", "failed"]
    assert b["items"][0]["error"] == "ComfyUI failed in KSampler: CUDA out of memory."
    assert fake.count("comfy POST /free") == 1 and app.llm.scheduler.running == []


async def test_a_stop_interrupts_the_item_and_leaves_the_rest_unmade(app):
    fake = app.fake
    fake.hang = True
    b = await app.batches.create("images", [{"prompt": "a"}, {"prompt": "b"}])
    app.batches.start(b["id"])
    await fake.queued.wait()
    assert app.llm.scheduler.running and app.llm.scheduler.running[0].exclusive
    assert await app.batches.stop(b["id"]) is True
    b = await batches_mod.get(app.store, b["id"])
    assert b["state"] == "stopped" and [i["state"] for i in b["items"]] == ["stopped"] * 2
    assert fake.bodies["comfy /interrupt"] == [{"prompt_id": "p1"}]
    assert fake.bodies["comfy /queue"] == [{"delete": ["p1"]}]
    assert fake.count("comfy POST /free") == 1 and app.llm.scheduler.running == []
    assert await app.batches.stop(b["id"]) is False, "not running any more"


async def test_a_restart_stops_what_it_cut(app, store):
    b = await app.batches.create("images", [{"prompt": "a"}])
    assert await batches_mod.sweep(store) == 1
    b = await batches_mod.get(store, b["id"])
    assert b["state"] == "stopped" and b["items"][0]["state"] == "stopped"


async def test_music_turns_the_instrumental_lora_off_for_a_song(app):
    b = await app.batches.create("music", [
        {"style": "dream pop, airy female vocals", "lyrics": "[Verse 1]\nla",
         "instrumental": False, "title": "Glass Rain", "duration": 120}])
    b = await app.batches.run(b["id"])
    [item] = b["items"]
    assert item["state"] == "done" and item["attachment"]["filename"].startswith("glass_rain-")
    g = app.fake.graphs[0]
    assert g["14"]["inputs"]["lora_1"]["on"] is False
    assert g["10"]["inputs"]["max_duration"] == 120.0
    assert g["10"]["inputs"]["seed"] == g["5"]["inputs"]["seed"] == item["seed"]


async def test_one_lease_per_gpu_and_a_busy_gpu_fails_the_rest_at_once(app, monkeypatch):
    from celine.backends.queue import QueueTimeout
    s = app.llm.router.s
    monkeypatch.setitem(s.providers, "comfy2", {**s.providers["comfy"], "label": "ComfyUI 2"})
    leases, busy = [], False

    @contextlib.asynccontextmanager
    async def gpu_lease(pid, **_):
        leases.append(pid)
        if busy:
            raise QueueTimeout("busy")
        yield
    monkeypatch.setattr(app.llm, "gpu_lease", gpu_lease)

    async def note(*_a, **_k):
        pass
    b = {"kind": "images", "conversation_id": None}
    async with contextlib.AsyncExitStack() as stack:
        servers = batches_mod._Servers(app.batches, b, stack, None)
        await servers.get("comfy", note)
        await servers.get("comfy2", note)
    assert leases == ["swap"], "two servers on one GPU: one lease"
    leases.clear()
    busy = True
    async with contextlib.AsyncExitStack() as stack:
        servers = batches_mod._Servers(app.batches, b, stack, None)
        for pid in ("comfy", "comfy2", "comfy"):
            with pytest.raises(presets.ImageError, match="stayed busy with llama-swap"):
                await servers.get(pid, note)
    assert leases == ["swap"], "the GPU is asked once, the rest fail at once"


async def test_music_made_outside_a_chat_says_nothing_of_hearing_it(app, store):
    skill = MusicGenerator(app.cfg)
    skill.app = app
    for kind, said in (("background", False), ("chat", True)):
        ctx, _ = await _call(store, await conversations.create(store, kind=kind), "m1",
                             "music_generator__generate")
        r = await skill.generate(ctx, items=[{"style": "ambient"}])
        assert ("hears each one in this conversation" in r.content) is said, kind
        await skill.stop(ctx, batch=r.content.split()[1])


# ---- its call ------------------------------------------------------------------------

async def test_her_call_returns_at_once_fills_in_place_and_never_wakes_her(app, store):
    skill = ImageGenerator(app.cfg)
    skill.app = app
    reg = Registry()
    reg.add(skill)
    from celine.turns import loads
    cid = await conversations.create(store)
    await loads.touch(store, cid, "tool", ["image_generator__generate"])
    await user_says(store, cid, "draw two")
    turn = await turns.claim(store, "w")
    call = ("image_generator__generate",
            json.dumps({"items": [{"prompt": "1girl"}, {"prompt": "2girls"}]}))
    async with anyio.create_task_group() as tg:
        jobs = Jobs(store, app.cfg, lambda: None, tg)
        app.fake.hang = True                  # still at work when the turn is done
        await run_turn(turn, store, app.cfg, Scripted([call]), EventBus(),
                       Token(turn["id"]), reg, jobs)
        [said] = await store.read(lambda c: [r[0] for r in c.execute(
            "SELECT content FROM messages WHERE conversation_id=? AND role='tool'",
            (cid,))])
        assert said.startswith("Batch ") and "started: 2 images" in said
        assert not jobs.handles, "a batch is no job"
        [b] = await batches_mod.list_batches(store, "images", conversation=cid)
        assert b["state"] == "running"
        app.fake.hang = False
        with anyio.fail_after(3):
            await app.batches.wait(b["id"])
        tg.cancel_scope.cancel()
    row = await store.read(lambda c: dict(c.execute(
        "SELECT state, display FROM tool_calls").fetchone()))
    display = json.loads(row["display"])
    assert row["state"] == "ok" and [i["state"] for i in display["items"]] == ["done"] * 2
    # it fills in place, quietly: no job, no message, no new turn
    assert await store.read(lambda c: c.execute(
        "SELECT (SELECT count(*) FROM jobs) + (SELECT count(*) FROM intercom WHERE kind='job') "
        "+ (SELECT count(*) FROM turns WHERE state='queued')").fetchone()[0]) == 0
    events = await store.read(lambda c: [r[0] for r in c.execute(
        "SELECT type FROM events WHERE conversation_id=?", (cid,))])
    assert events.count("images.batch") >= 4, "every change is an event"


async def test_an_edit_starts_from_her_images_never_from_yours_elsewhere(app, store):
    skill = ImageGenerator(app.cfg)
    skill.app = app
    here, other = await conversations.create(store), await conversations.create(store)
    ctx, _ = await _call(store, here, "c1")

    # an image you sent in another conversation is not its own to hand over here
    up = await attachments.add(store, "f" * 64, 10, "image/png", "you.png", "image")
    await user_says(store, other, "mine", attachment_ids=[up["id"]])
    r = await skill.generate(ctx, items=[{"prompt": "make it night", "source": up["id"]}])
    assert r.is_error and "that you may use" in r.content and app.fake.graphs == []

    # its own image from any conversation is fine; it belongs to the call that made it
    made_ctx, mid = await _call(store, other, "c2")
    made = await skill.generate(made_ctx, items=[{"prompt": "1girl"}], wait=True)
    assert "is done: 1 of 1 made" in made.content and not made.is_error
    [b] = await batches_mod.list_batches(store, "images", conversation=other)
    img = b["items"][0]["attachment"]
    assert img["id"] in [r["id"] for r in await attachments.images(store, other)]
    r = await skill.generate(ctx, items=[{"prompt": "make it night",
                                          "source": img["id"][:8]}])
    item = (await batches_mod.list_batches(store, "images", conversation=here))[0]["items"][0]
    assert not r.is_error and item["source"]["id"] == img["id"]
    assert item["preset"] == "krea2-identity-edit", "a source makes it an edit"
    listed = (await skill.list_images(made_ctx)).content
    assert f"{img['id'][:8]}" in listed and "prompt: 1girl" in listed


async def test_an_edit_of_three_images_finds_the_preset_that_takes_them(app, store):
    import hashlib
    skill = ImageGenerator(app.cfg)
    skill.app = app
    cid = await conversations.create(store)
    ids = []
    for n in range(3):
        data = png(800 + n, 1200)
        sha = hashlib.sha256(data).hexdigest()
        path = attachments.blob_path(app.cfg, sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        ids.append((await attachments.add(store, sha, len(data), "image/png",
                                          f"{n}.png", "image"))["id"])
    await user_says(store, cid, "these", attachment_ids=ids)
    ctx, _ = await _call(store, cid, "c1")
    prompt = "Dress the woman in image 1 in the coat from image 2, on the bridge in image 3."
    r = await skill.generate(ctx, items=[{"prompt": prompt, "source": ids[0][:8],
                                          "source2": ids[1][:8], "source4": ids[2][:8]}],
                             wait=True)
    [item] = (await batches_mod.list_batches(store, "images", conversation=cid))[0]["items"]
    assert not r.is_error and item["state"] == "done"
    assert item["preset"] == "moss-image-edit", "the first editor takes one image"
    assert [item[k]["id"] for k in ("source", "source2", "source3")] == ids
    assert "source4" not in item
    assert (item["width"], item["height"]) == (832, 1248), "the first image's shape"
    assert "source, source2, source4 became source, source2, source3" in r.content
    assert f"Edited from {ids[0][:8]}, {ids[1][:8]} and {ids[2][:8]}." in r.content
    assert app.fake.count("comfy POST /upload/image") == 3
    graph = app.fake.graphs[-1]
    edit = graph["6"]["inputs"]
    assert edit["prompt"] == prompt and edit["width"] == 832
    assert "5" not in graph and "reference_4" not in edit, "a missing image's node goes"
    assert edit["reference_3"] == ["4", 0] and graph["4"]["inputs"]["image"] == \
        "celine-src.png"

    r = await skill.generate(ctx, items=[{"prompt": "x", "preset": "krea2-identity-edit",
                                          "source": ids[0][:8], "source2": ids[1][:8]}])
    assert r.is_error and "Krea 2 identity edit takes 1 source image. 2 images: " \
        "moss-image-edit or qwen2.1-image-edit." in r.content


async def test_a_lora_is_checked_against_the_catalog_and_a_broken_one_says_why(app):
    item = {"prompt": "a", "preset": "krea2-turbo", "loras": ["krea_2/nope"]}
    b = await app.batches.create("images", [item])
    assert "Krea 2 turbo has no LoRA 'krea_2/nope'." in b["items"][0]["error"]
    from celine import image_catalog
    image_catalog._cache.clear()
    app.fake.lm = 500
    b = await app.batches.create("images", [item])
    assert b["items"][0]["error"] == "ComfyUI did not list its models (HTTPStatusError)."



# ---- the routes ----------------------------------------------------------------------

async def test_the_routes_list_presets_and_run_a_batch_of_the_user(app, store):
    from fastapi import FastAPI
    from celine.errors import install_error_handler
    web = FastAPI()
    install_error_handler(web)
    web.include_router(comfy_api.router)
    web.state.services = app
    async with client(web) as c:
        got = (await c.get("/api/generation-presets?kind=images")).json()["presets"]
        krea = next(p for p in got if p["id"] == "krea2-turbo")
        assert krea["ref"] == "comfy/krea2-turbo" and "workflow" not in krea
        qwen = next(p for p in got if p["id"] == "qwen2.1-image-edit")
        assert qwen["ref"] is None, "a preset no role names has no ref"
        assert (await c.get("/api/generation-presets?kind=music")).json()["presets"][0]["id"] == "yue2"

        r = await c.post("/api/batches", json={"kind": "images", "items": [{"prompt": "1girl"}]})
        bid = r.json()["id"]
        assert r.json()["conversation_id"] is None
        async def ended():
            return (await c.get(f"/api/batches/{bid}")).json()["state"] != "running"
        await until(ended, 3)
        b = (await c.get(f"/api/batches/{bid}")).json()
        assert b["items"][0]["state"] == "done"
        assert (await c.get("/api/batches?kind=images")).json()["batches"][0]["id"] == bid
        assert (await c.post(f"/api/batches/{bid}/stop")).json() == {"stopped": False}
        assert (await c.get("/api/batches?kind=music")).json()["batches"] == []
        r = await c.post("/api/batches", json={"kind": "images", "items": [{"prompt": "a"}] * 3,
                                                       "count": 6})
        assert r.status_code == 400 and r.json()["error"]["message"] == (
            "A batch holds at most 16 images (items times count); this one asks for 18.")
    # the user's batch has no message: its image stays (gc keeps it)
    att = b["items"][0]["attachment"]
    await attachments.gc(app.cfg, store, at=datetime(2100, 1, 1, tzinfo=timezone.utc))
    assert await attachments.get(store, att["id"]) is not None


# ---- preset files --------------------------------------------------------------------

def test_every_shipped_preset_loads_and_a_broken_one_says_why(tmp_path):
    for kind in presets.FOLDERS:
        found, problems = presets.load(kind)
        assert found and problems == []
    assert presets.of("images")["krea2-turbo"].default("mode") == "direct"
    (tmp_path / "bad.json").write_text(json.dumps({"1": {"class_type": "X", "inputs": {}}}))
    (tmp_path / "bad.toml").write_text(
        'label = "Bad"\nfamily = "X"\ngood_at = "x"\nprompting = "x"\nexample = "x"\n'
        'workflow = "bad.json"\ncolour = "red"\n'
        '[parameters.prompt]\ninputs = [["1", "text"]]\n'
        '[parameters.wings]\ninputs = [["1", "w"]]\n')
    found, problems = presets.load_dir("images", tmp_path)
    assert found == {}
    said = [p.message for p in problems]
    assert "colour is not a preset setting." in said
    assert "parameters.prompt: the workflow has no input 1 > text." in said
    assert any(s.startswith("parameters.wings is not a parameter") for s in said)
    assert "parameters.seed is missing." in said
    assert all(p.file == f"config/{tmp_path.name}/bad.toml" for p in problems)


async def test_her_guide_gets_the_presets_from_their_files(app):
    docs = ImageGenerator(app.cfg).docs["presets"]
    assert "### `anima-turbo`: Anima turbo (Anima), new images, the default" in docs
    assert "—" not in docs
    assert "YuE2" in MusicGenerator(app.cfg).docs["presets"]


async def test_an_unknown_preset_errors_and_lists_the_presets(app):
    gen = ImageGenerator(app.cfg)
    gen.app = app
    ctx = ToolContext("c", "t", "x", app.store, None, None)  # type: ignore[arg-type]
    for name in ("nope", "KREA2-TURBO"):
        out = await gen.list_presets(ctx, kind="lora", preset=name)
        assert out.is_error and "krea2-turbo" in out.content, name


async def test_her_item_is_taken_as_given_and_what_does_not_fit_fails(app):
    b = await app.batches.create("images", [
        "a cat on a roof",
        {"prompt": "b", "variant": "krea2-turbo"},
        {"prompt": "c", "size": "832x1216", "steps": 20},
        {"prompt": "d", "seed": "7", "variant": "expand"},
        {"prompt": "e", "preset": "anima-turbo", "variant": "expand"},
    ])
    one, two, three, four, five = b["items"]
    assert one["state"] == "waiting" and one["prompt"] == "a cat on a roof"
    assert two["error"] == "Anima turbo has no variant 'krea2-turbo': it has none.", \
        "no preset has it: the first one says why"

    assert "Field 'size' is not an item field" in three["error"]
    assert "Field 'steps' is not an item field" in three["error"]
    assert (four["state"], four["preset"], four["seed"]) == ("waiting", "krea2-turbo", 7), \
        "no preset named: the first that has the variant"
    assert five["error"] == "Anima turbo has no variant 'expand': it has none."


async def test_her_prompt_reaches_the_workflow_byte_for_byte(app):
    prompt = "  1girl, camera, photo, 16:9 widescreen, 4k,, wallpaper size\n\"quoted\"  "
    b = await app.batches.create("images", [{"prompt": prompt}])
    assert b["items"][0]["prompt"] == prompt and "warnings" not in b["items"][0]
    b = await app.batches.run(b["id"])
    assert b["items"][0]["state"] == "done"
    assert app.fake.graphs[0]["4"]["inputs"]["text"] == prompt


def test_shares_gpu_with_names_a_llama_swap_provider():
    from celine.settings.files import Invalid
    from celine.settings.registry import validate_providers
    assert validate_providers(PROVIDERS)["comfy"]["options"]["shares_gpu_with"] == "swap"
    with pytest.raises(Invalid):
        validate_providers({**PROVIDERS, "swap": {**PROVIDERS["swap"], "kind": "ollama"}})


def test_a_lora_chain_and_a_model_go_into_the_graph():
    p = presets.of("images")["krea2-turbo"]
    graph = {"prompt": "x", "seed": 1, "width": 64, "height": 64}
    cat = Catalog([
        Resource("model", "krea2/turbo/krea2_turbo_nvfp4.safetensors", "Krea 2 turbo", "base",
                 turbo=True),
        Resource("model", "krea2/krea2_raw_bf16.safetensors", "Krea 2 raw", "base",
                 turbo=False, settings={"steps": 30}),
        Resource("lora", "krea_2/custom/my_character_v1.safetensors", "My character", "character",
                 triggers=("mychar",), strength=0.9),
        Resource("lora", "krea_2/candid.safetensors", "Candid", "realism"),
    ])
    c = presets.choose(p, cat, model=None, loras=["my_character_v1", "krea_2/candid:2"])
    g = presets.attach(p.build(graph), p, c)
    assert g["celine_lora_1"]["inputs"]["model"] == ["3", 0]
    assert g["celine_lora_2"]["inputs"]["model"] == ["celine_lora_1", 0]
    assert g["8"]["inputs"]["model"] == ["celine_lora_2", 0]
    assert g["celine_lora_1"]["inputs"]["strength_model"] == 0.9      # its default
    assert g["celine_lora_2"]["inputs"]["strength_model"] == 2        # its own, as given
    assert g["5"]["inputs"]["text"] == ["celine_triggers", 0]
    assert g["celine_triggers"]["inputs"]["string_a"] == "mychar"
    assert c.recipe()["loras"] == ["krea_2/custom/my_character_v1:0.9", "krea_2/candid:2"]

    c = presets.choose(p, cat, model="krea2/krea2_raw_bf16", loras=None)
    g = presets.attach(p.build({**graph, "mode": "expand"}), p, c)
    assert g["3"]["inputs"]["unet_name"] == "krea2/krea2_raw_bf16.safetensors"
    assert g["8"]["inputs"]["steps"] == 30, "the model's own setting"
    assert g["8"]["inputs"]["cfg"] == 3.5, "the rest of the preset's full settings"
    assert g["2"]["inputs"]["mode"] == "expand", "a mode word becomes the node's value"
    assert "celine_triggers" not in g


# ---- the user's presets (Settings, Images & Music) -------------------------------------

UPLOAD = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}", "clip": ["4", 1]}},
    "2": {"class_type": "EmptyLatentImage", "inputs": {"width": "{{width:640}}",
                                                       "height": "{{ height:512 }}"}},
    "3": {"class_type": "KSampler", "inputs": {"seed": "{{seed}}", "steps": 8,
                                               "positive": ["1", 0], "latent_image": ["2", 0]}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "flux.safetensors"}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["3", 0]}},
}


@contextlib.asynccontextmanager
async def _web(app):
    from fastapi import FastAPI
    from celine.errors import install_error_handler
    web = FastAPI()
    install_error_handler(web)
    from celine.api import presets as presets_api
    web.include_router(comfy_api.router)
    web.include_router(presets_api.router)
    web.state.services = app
    app.registry = app.registry or build_registry(app)
    async with client(web) as c:
        yield c


def _user_files(kind="images"):
    root = presets.user_folder(kind)
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


async def test_an_uploaded_workflow_is_a_preset_her_batch_runs_end_to_end(app):
    async with _web(app) as c:
        r = await c.post("/api/generation-presets", json={
            "kind": "images", "label": "My Flux", "workflow": json.dumps(UPLOAD)})
        assert r.status_code == 201, r.text
        mine = r.json()
        assert mine["id"] == "my-flux" and mine["origin"] == "own" and mine["task"] == "generate"
        assert mine["size"]["width"] == 640 and mine["size"]["height"] == 512
        assert mine["ref"] == "comfy/my-flux", "on at once, at the end of its role"
        assert _user_files() == ["my-flux.json", "my-flux.toml"]
        assert "my-flux" in ImageGenerator(app.cfg).docs["presets"]

        r = await c.post("/api/batches", json={"kind": "images", "items": [
            {"prompt": "a cat on a windowsill", "preset": "my-flux", "seed": 7}]})
        bid = r.json()["id"]

        async def ended():
            return (await c.get(f"/api/batches/{bid}")).json()["state"] != "running"
        await until(ended, 3)
        assert (await c.get(f"/api/batches/{bid}")).json()["items"][0]["state"] == "done"
        g = app.fake.graphs[-1]
        assert g["1"]["inputs"]["text"] == "a cat on a windowsill"
        assert g["3"]["inputs"]["seed"] == 7
        assert (g["2"]["inputs"]["width"], g["2"]["inputs"]["height"]) == (640, 512)

        # the download is the upload again: its placeholders, with their defaults
        down = await c.get("/api/generation-presets/my-flux/workflow")
        assert 'filename="my-flux.json"' in down.headers["content-disposition"]
        back = down.json()
        assert back["1"]["inputs"]["text"] == "{{prompt}}"
        assert back["2"]["inputs"]["width"] == "{{width:640}}"
        r = await c.post("/api/generation-presets", json={
            "kind": "images", "label": "My Flux", "workflow": down.text})
        assert r.json()["id"] == "my-flux-2"

        r = await c.delete("/api/generation-presets/my-flux")
        assert r.json() == {"deleted": True}
        assert "comfy/my-flux" not in app.cfg.settings.chain("image_generation")
        assert "my-flux" not in app.cfg.settings.providers["comfy"]["models"]
        assert "my-flux.toml" not in _user_files()


async def test_an_upload_names_each_missing_placeholder_and_saves_nothing(app):
    wf = {k: json.loads(json.dumps(v)) for k, v in UPLOAD.items()}
    wf["3"]["inputs"]["seed"] = 42
    wf["1"]["inputs"]["clip_l"] = "{{promt}}"
    async with _web(app) as c:
        r = await c.post("/api/generation-presets", json={
            "kind": "images", "label": "Broken", "workflow": json.dumps(wf)})
    assert r.status_code == 400
    said = r.json()["error"]["message"]
    assert "{{seed}} is missing: put it as the value of the input that takes the seed." in said
    assert "{{promt}} is not a placeholder of image presets." in said
    assert _user_files() == []
    assert "broken" not in presets.of("images")


async def test_the_ui_format_is_refused_with_how_to_export_the_api_format(app):
    graph = {"last_node_id": 9, "nodes": [{"id": 1, "type": "KSampler"}], "links": []}
    async with _web(app) as c:
        r = await c.post("/api/generation-presets", json={
            "kind": "music", "label": "Song", "workflow": json.dumps(graph)})
    assert r.status_code == 400
    assert "Export (API)" in r.json()["error"]["message"]
    assert _user_files("music") == []


async def test_a_shipped_preset_is_edited_and_reset_to_shipped(app):
    async with _web(app) as c:
        got = (await c.get("/api/generation-presets?kind=images")).json()["presets"]
        krea = next(p for p in got if p["id"] == "krea2-turbo")
        assert krea["origin"] == "shipped" and krea["values"]["default.mode"] == "direct"
        assert {f["key"] for f in krea["fields"]} >= {"label", "default.width", "variants",
                                                      "catalog.loras", "catalog.full.steps"}
        values = {**krea["values"], "label": "Krea fast", "default.width": 1024,
                  "default.mode": "gently", "catalog.full.steps": "30"}
        r = await c.put("/api/generation-presets/krea2-turbo", json={"values": values})
        assert r.status_code == 200, r.text
        edited = r.json()
        assert edited["origin"] == "edited" and edited["label"] == "Krea fast"
        assert edited["size"]["width"] == 1024 and edited["values"]["default.mode"] == "gently"
        p = presets.of("images")["krea2-turbo"]
        assert p.workflow["2"]["inputs"]["mode"] == "expand gently"
        assert p.full_settings == {"steps": 30, "cfg": 3.5}
        assert sorted(_user_files()) == ["krea2-turbo.json", "krea2-turbo.toml"]

        r = await c.put("/api/generation-presets/krea2-turbo",
                        json={"values": {**values, "default.mode": "loud"}})
        assert r.status_code == 400 and "loud is not a variant" in r.json()["error"]["message"]
        r = await c.put("/api/generation-presets/krea2-turbo/workflow",
                        json={"workflow": json.dumps({"2": {"class_type": "X", "inputs": {
                            "brief": "{{prompt}}", "seed": "{{seed}}", "width": "{{width:1}}",
                            "height": "{{height:1}}"}}})})
        assert r.status_code == 400
        assert "catalog.lora_after: the workflow has no node 3." in r.json()["error"]["message"]
        assert presets.of("images")["krea2-turbo"].label == "Krea fast", "nothing half-saved"

        r = await c.post("/api/generation-presets/krea2-turbo/reset")
        assert r.json()["origin"] == "shipped" and r.json()["label"] == "Krea 2 turbo"
        assert _user_files() == []
        assert presets.of("images")["krea2-turbo"].default("mode") == "direct"
        r = await c.delete("/api/generation-presets/krea2-turbo")
        assert r.status_code == 400, "a shipped preset is reset, never deleted"


async def test_a_batch_call_while_the_batches_are_down_is_an_error_result():
    from types import SimpleNamespace as NS

    from celine.skills.images import batch_generate, batch_stop
    app, ctx = NS(batches=None), NS(conversation_id="c", tool_call_id="t", time_left=None)
    got = await batch_generate(app, ctx, "images", [{"prompt": "a cat"}], 1, False,
                               lambda b, started: "")
    assert (got.is_error, got.content) == (True, "Images are off.")
    got = await batch_stop(app, ctx, "music", "b1", lambda b, started: "")
    assert (got.is_error, got.content) == (True, "Music is off.")
