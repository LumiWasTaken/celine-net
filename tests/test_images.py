"""Images and the model that answers (attachments.py): a text-only model gets the
image's photo description, written once by the Photo descriptions role and kept
with the attachment."""
from __future__ import annotations

from chat_fixtures import PNG, upload, user_says

from types import SimpleNamespace

import httpx
import pytest

from celine.conversations import store as conversations
from fake_backend import FakeBackend
from celine.turns import context
from celine import attachments
from celine.backends.base import Done, TextDelta
from celine.backends.llm import LLM
from celine.backends.queue import Priority
from celine.backends.router import Router
from celine.boot import load
from settings_fixtures import scratch

pytestmark = pytest.mark.anyio

PROVIDERS = {
    "main": {"kind": "ollama", "label": "gpu-server", "base_url": "http://main:11434/v1",
             "models": {"eyes": {"label": "Eyes", "caps": ["chat", "vision"]},
                        "glm": {"label": "GLM 5.2", "caps": ["chat", "tools"]}}},
    "lan": {"kind": "ollama", "label": "gpu-box", "base_url": "http://lan:11434/v1",
            "models": {"photo": {"label": "small-model", "caps": ["chat", "vision"]}}},
}
CAPTION = '{"caption": "A red square.", "visible_text": "STOP"}'


class Model:
    """A model server's answer for one model: records what each request carried."""

    def __init__(self, ref: str, url: str, name: str, sees: bool, script):
        self.name, self.model = ref, name
        self.cfg = SimpleNamespace(id=ref, base_url=url, raw={}, label=name)
        self.supports_tools, self.supports_images = True, sees
        self.script = script
        self.seen: list[list[dict]] = []

    async def stream(self, messages, tools, body=None):
        self.seen.append(messages)
        for ev in self.script:
            yield ev


class Servers:
    def __init__(self):
        self.down: set[str] = set()

    def reply(self, request):
        if request.url.host in self.down:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"models": []})


def setup(tmp_path, monkeypatch, roles):
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles=roles)
    cfg = load()
    cfg.llm = {"queue": {"wait_seconds": 5, "background_wait_seconds": 5}}
    servers = Servers()
    router = Router(cfg, transport=httpx.MockTransport(servers.reply))
    models: dict[str, Model] = {}
    for pid, p in PROVIDERS.items():
        for name, m in p["models"].items():
            ref = f"{pid}/{name}"
            script = [TextDelta(CAPTION if ref == "lan/photo" else "ok"), Done(reason="stop")]
            models[ref] = Model(ref, p["base_url"], name, "vision" in m["caps"], script)
    llm = LLM(cfg, build=lambda bc: models[bc.id], router=router)
    return cfg, llm, models, servers


async def image_message(cfg, store, text="what is this?"):
    img = await upload(cfg, store, PNG, "dot.png", "image/png")
    cid = await conversations.create(store)
    await user_says(store, cid, text, attachment_ids=[img["id"]])
    return cid, img["id"]


async def ask(cfg, store, llm, cid):
    """One request of a turn, as the runner sends it."""
    built = await context.build(store, cfg, FakeBackend(), cid, system="", tools=[])
    msgs = [m for m in built.messages if m["role"] != "system"]
    qb = llm.bind(role="chat", priority=Priority.INTERACTIVE, conversation_id=cid,
                  images=attachments.Images(cfg, store, llm, lane=cid))
    events = qb.stream(msgs, [])
    try:
        return [ev async for ev in events]
    finally:
        await events.aclose()


async def test_a_text_only_model_gets_the_description_once_written_and_reused(
        tmp_path, monkeypatch, store):
    cfg, llm, models, _ = setup(tmp_path, monkeypatch, {
        "chat": {"chain": ["main/glm"]}, "vision": {"chain": ["lan/photo"]}})
    cid, aid = await image_message(cfg, store)
    await ask(cfg, store, llm, cid)
    sent = models["main/glm"].seen[-1][-1]["content"]
    assert "what is this?" in sent and "A red square." in sent
    photo = models["lan/photo"].seen[0][0]["content"]
    assert photo[1]["image_url"]["url"].startswith("data:image/png;base64,")
    row = await attachments.get(store, aid)
    assert (row["description"], row["described_by"]) == (
        'A red square. Text in the image: "STOP"', "small-model")
    # the next request reuses it: the photo model is not asked again
    await ask(cfg, store, llm, cid)
    assert models["main/glm"].seen[-1][-1]["content"] == sent
    assert len(models["lan/photo"].seen) == 1


async def test_only_the_newest_images_go_as_pixels_older_ones_as_their_description(
        tmp_path, monkeypatch, store):
    """Three messages with an image each, a model with eyes: the first image goes as
    its description (written once by the photo model), the last two as pixels."""
    cfg, llm, models, _ = setup(tmp_path, monkeypatch, {
        "chat": {"chain": ["main/eyes"]}, "vision": {"chain": ["lan/photo"]}})
    from celine.conversations import tree
    from celine.turns import store as turns
    cid = await conversations.create(store)
    for n in (1, 2, 3):
        img = await upload(cfg, store, PNG, f"dot{n}.png", "image/png")
        mid, tid, _ = await user_says(store, cid, f"picture {n}", attachment_ids=[img["id"]])
        if n < 3:                      # an exchange each: the request holds all three
            reply = await tree.start_assistant_message(store, cid, mid, tid)
            await tree.finish_assistant_message(store, cid, reply, f"seen {n}")
            await turns.transition(store, tid, cid, "done")
    await ask(cfg, store, llm, cid)
    sent = [m for m in models["main/eyes"].seen[-1] if m["role"] == "user"]
    assert isinstance(sent[0]["content"], str) and "A red square." in sent[0]["content"]
    for m in sent[1:]:
        assert any(p.get("type") == "image_url" for p in m["content"]), "pixels"
    assert len(models["lan/photo"].seen) == 1, "described once"
