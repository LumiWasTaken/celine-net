"""writer__draft (skills/writer.py): the request is built from its files, it runs on the
writer role with thinking off and its voice bans, and it fails when the role's model
fails, with no other model tried."""
from __future__ import annotations

import types

import pytest

from celine import boot
from celine.backends.base import BackendError, Done, TextDelta
from celine.backends.llm import LLM
from celine.backends.openai import LlamaServerBackend
from celine.backends.router import ProviderState
from celine.settings import files, seed
from celine.skills import ToolContext
from celine.skills.writer import Writer, messages
from settings_fixtures import scratch

pytestmark = pytest.mark.anyio

PROVIDERS = {
    "main": {"kind": "llama-swap", "base_url": "http://main:11434/v1",
             "models": {"gemma": {"caps": ["chat"]}, "big": {"caps": ["chat", "tools"]}}},
}
ROLES = {"chat": {"chain": ["main/big"]}, "writer": {"chain": ["main/gemma", "main/big"]}}


class Rec(LlamaServerBackend):
    """Keeps the body each call would send and answers with a dash in it; `fail` makes
    the call fail before its first token."""
    bodies: list[dict] = []
    fail = False

    async def stream(self, messages, tools, body=None, model=None):
        Rec.bodies.append(self.request_body(messages, tools, body, model=model))
        if Rec.fail:
            raise BackendError("the model is gone")
        yield TextDelta(text="lol same — my cat hates baths too")
        yield Done(reason="stop")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles=ROLES)
    cfg = boot.load()
    cfg.llm = {"queue": {"wait_seconds": 5, "background_wait_seconds": 5}}
    root = cfg.her.notebook
    root.mkdir(parents=True, exist_ok=True)
    (root / "identity.md").write_text("# who you are\nyou are {{name}}, {{username}}'s.\n")
    (root / "samples.md").write_text(
        "load: always\nHow I talk:\n\n### 2026-10-01\nnew sample\n\n## earlier\n\n"
        "### 2026-09-01\nold sample\n")
    cfg.her.talk = "short. lowercase.\n"
    return cfg


def _app(cfg, monkeypatch):
    llm = LLM(cfg, build=lambda bc: Rec(bc, 5))

    async def probe(pid, **_):
        return ProviderState(reachable=True, loaded=frozenset())
    monkeypatch.setattr(llm.router, "probe", probe)
    Rec.bodies, Rec.fail = [], False
    w = Writer(cfg)
    w.app = types.SimpleNamespace(llm=llm)
    return w


def _ctx(cfg):
    return ToolContext(conversation_id="c1", turn_id="t1", tool_call_id="x", store=None,
                       config=cfg, emit=None)  # type: ignore[arg-type]


def test_the_request_is_her_persona_identity_talk_and_samples(cfg):
    system, user = messages(cfg, "say the cat is cute", "post", "r/cats: my cat")
    me = cfg.her
    s = system["content"]
    assert cfg.substitute(me.persona).strip() in s
    assert f"you are {me.name}" in s and "{{" not in s
    assert "### how you talk\nshort. lowercase." in s
    assert "new sample" in s and "old sample" not in s, "the earlier samples stay in the file"
    assert s.index("how you talk") < s.index("### samples.md")
    assert user["content"].startswith("Write a post: its title on the first line")
    assert "Brief: say the cat is cute" in user["content"]
    assert "r/cats: my cat" in user["content"]


async def test_it_runs_on_the_writer_role_and_returns_only_the_text(cfg, monkeypatch):
    w = _app(cfg, monkeypatch)
    got = await w.draft(_ctx(cfg), brief="agree about the cat", kind="comment",
                        context="r/cats")
    assert not got.is_error
    assert got.content == "lol same \u2014 my cat hates baths too", "its text, as written"
    assert got.display == {"kind": "comment", "model": "gemma", "chars": len(got.content)}
    body = Rec.bodies[-1]
    assert body["model"] == "gemma"
    assert body["reasoning_effort"] == "none", "thinking off"
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    assert body["logit_bias"], "its voice bans"
    assert body["messages"][0]["role"] == "system"


async def test_it_fails_when_the_writer_model_fails(cfg, monkeypatch):
    w = _app(cfg, monkeypatch)
    Rec.fail = True
    got = await w.draft(_ctx(cfg), brief="hi", kind="message")
    assert got.is_error and "the model is gone" in got.content
    assert [b["model"] for b in Rec.bodies] == ["gemma"], "no other model is tried"


async def test_an_empty_writer_role_fails_the_call(cfg, monkeypatch):
    w = _app(cfg, monkeypatch)
    cfg.settings.roles["writer"] = {"chain": []}
    got = await w.draft(_ctx(cfg), brief="hi", kind="message")
    assert got.is_error and not Rec.bodies


def test_the_seed_leaves_an_install_without_llama_swap(tmp_path):
    root = tmp_path / "settings"
    files.write(root, "roles", {"chat": {"chain": []}})
    seed.seed(root)
    assert "writer" not in files.read(root, "roles")[0]
