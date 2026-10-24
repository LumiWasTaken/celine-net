"""Banned words (settings/voice.py): the shipped list, the request bodies of chat calls
with its voice: on tabbyAPI its phrases and Banned words as banned_strings, on
llama-server its Banned words at its Bias as logit_bias, none when off, on another
server or for other roles; merged with a base."""
from __future__ import annotations

import tomllib

import pytest

import celine.settings as st
from celine import boot
from celine import config as config_mod
from celine.backends.base import Done
from celine.backends.llm import LLM
from celine.backends.openai import LlamaServerBackend
from celine.backends.router import ProviderState
from celine.config import BackendConfig
from celine.settings import voice
from settings_fixtures import scratch

pytestmark = pytest.mark.anyio

PROVIDERS = {
    "main": {"kind": "tabby", "base_url": "http://main:11434/v1",
             "models": {"big": {"caps": ["chat", "tools"]},
                        "eyes": {"caps": ["chat", "vision"]}}},
    "lan": {"kind": "llama-server", "base_url": "http://lan:8080/v1",
            "models": {"small": {"caps": ["chat", "tools"]}}},
}
ROLES = {"chat": {"chain": ["main/big"]}, "vision": {"chain": ["main/eyes"]},
         "tasks": {"chain": ["main/big"]}}


class Rec(LlamaServerBackend):
    """Keeps the body each call would send instead of sending it."""
    bodies: list[dict] = []

    async def stream(self, messages, tools, body=None, model=None):
        Rec.bodies.append(self.request_body(messages, tools, body, model=model))
        yield Done(reason="stop")


@pytest.fixture
def llm(tmp_path, monkeypatch):
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles=ROLES)
    cfg = boot.load()
    cfg.llm = {"queue": {"wait_seconds": 5, "background_wait_seconds": 5}}
    out = LLM(cfg, build=lambda bc: Rec(bc, 5))

    async def probe(pid, **_):
        return ProviderState(reachable=True, loaded=frozenset())
    monkeypatch.setattr(out.router, "probe", probe)
    Rec.bodies = []
    return out


async def body_of(llm, role, cid=None, **kw):
    async for _ in llm.bind(role=role, conversation_id=cid, **kw).stream(
            [{"role": "user", "content": "hi"}], []):
        pass
    return Rec.bodies[-1]


async def test_the_shipped_list_loads_and_is_hers(tmp_path, monkeypatch, store):
    shipped = voice.validate(tomllib.loads(
        (config_mod.CONFIG_DIR / "her" / "voice.toml").read_text()))
    assert shipped["on"] and shipped["bias"] == -8.0
    assert {" delve", " leverage", " Moreover", " nestled"} <= set(shipped["banned"])
    var = scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    assert cfg.her.voice == shipped
    assert not (var / "settings" / "voice.toml").exists(), "no voice in var/settings"
    from celine import her
    await her.write(store, cfg, {"voice": {"banned": [" utilize"]}})
    assert boot.load().her.voice["banned"] == [" utilize"], "never copied again"


async def test_a_chat_call_on_tabby_carries_her_phrases_and_bans_as_banned_strings(llm):
    voice = llm.cfg.her.voice
    assert voice["banned"][:2] == ["\u2014", "\u2013"], "the dashes are on the list"
    body = await body_of(llm, "chat", "c1")
    assert body["banned_strings"] == [*voice["phrases"], *voice["banned"]]
    assert "logit_bias" not in body


async def test_a_chat_call_on_llama_server_carries_her_bans_as_logit_bias(llm):
    llm.cfg.settings.roles["chat"] = {"chain": ["lan/small"]}
    body = await body_of(llm, "chat", "c1")
    assert body["logit_bias"] == [[b, -8.0] for b in llm.cfg.her.voice["banned"]]
    assert "banned_strings" not in body


async def test_a_task_conversation_carries_them_too(llm):
    st.CONVERSATION_ROLES.update(task1="tasks")      # a mission, a sub-agent, a run
    assert "banned_strings" in await body_of(llm, "chat", "task1")


async def test_a_vision_call_never_carries_them(llm):
    body = await body_of(llm, "vision")
    assert "banned_strings" not in body and "logit_bias" not in body


def test_a_bad_word_or_a_bad_weight_is_refused():
    for bad in ({"banned": [" "]}, {"banned": [""]}, {"banned": ["a\nb"]}, {"bias": -101},
                {"on": "yes"}, {"bans": []}, {"never": []}, {"patterns": ["(unclosed"]},
                {"phrases": [""]}):
        with pytest.raises(st.Invalid):
            voice.validate(bad)
    assert voice.validate({"banned": ["it is important to"]})["banned"] == \
        ["it is important to"], "several words are fine on tabbyAPI"
    assert voice.validate({}) == {"banned": [], "bias": -8.0, "on": True,
                                  "phrases": [], "patterns": []}


def test_off_or_another_server_or_nothing_banned_sends_none():
    for kind in ("llama-swap", "tabby"):
        bc = BackendConfig(id="m", kind="openai", label="M", base_url="http://m/v1",
                           model="big", raw={"provider_kind": kind})
        assert voice.apply(bc, voice.validate({"banned": [" delve"], "on": False})) is bc
        assert voice.apply(bc, voice.validate({})) is bc
    other = BackendConfig(id="m", kind="openai", label="M", base_url="http://m/v1",
                          model="big", raw={"provider_kind": "openai"})
    assert voice.apply(other, voice.validate({"banned": [" delve"]})) is other


def test_merge_keeps_the_base_and_adds_hers_once():
    base = {"banned": ["\u2014", " delve"], "bias": -8.0}
    got = voice.merge_voice(base, {"banned": ["moist", " delve"], "bias": -20})
    assert got == {"banned": ["\u2014", " delve", " moist"], "bias": -20}

