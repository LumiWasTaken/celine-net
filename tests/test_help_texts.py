"""Every "?" text the server sends (Help in the web kit): at most two sentences and 220
characters, ending with a period, no dashes or " · ", and no bare pronoun or agent name
(the agent's name is a placeholder the server fills)."""
from __future__ import annotations

import re

import pytest

from celine import boot
from celine.api import appearance as appearance_api
from celine.settings import appearance, features, general, registry, voice
from settings_fixtures import client, make_app, scratch

SENTENCE_END = re.compile(r"[.!?](?:\s|$)")
# a pronoun, the default name or "the agent" outside a {{placeholder}}
BARE = re.compile(r"(?<!\{\{)\b(she|her|hers|herself|celine|the agent)\b(?!\}\})",
                  re.IGNORECASE)


def _texts() -> list[tuple[str, str]]:
    out = [(f"role {k}", r["help"]) for k, r in registry.ROLES.items()]
    out += [("providers", registry.PROVIDERS_HELP)]
    out += [(f"option {k}", w["help"]) for k, w in registry.OPTION_WORDS.items() if w.get("help")]
    out += [(f"general {f['key']}", f["help"]) for f in general.fields() if f.get("help")]
    out += [(f"voice {k}", t) for k, t in voice.HELP.items()]
    out += [(f"appearance {k}", t) for k, t in appearance.HELP.items()]
    out += [(f"feature {f.id}", features.help_of(f)) for f in features.catalog()]
    out += [(f"core {i}", about) for i, _, about in features.CORE]
    return out


@pytest.mark.parametrize("where,text", _texts(), ids=lambda x: x if " " in x else "")
def test_each_help_text_is_short_plain_and_names_no_one(where, text):
    assert text.strip(), where
    assert len(text) <= 220, f"{where}: {len(text)} characters"
    assert len(SENTENCE_END.findall(text)) <= 2, f"{where}: more than two sentences"
    assert text.endswith("."), where
    for bad in ("—", "–", " · "):
        assert bad not in text, where
    assert not BARE.search(text), f"{where}: a bare name or pronoun"


def test_every_role_has_help():
    assert all(r.get("help") for r in registry.ROLES.values())


def test_every_skill_knob_has_help():
    from celine.skills import knob_fields
    assert [f["key"] for _, f in knob_fields() if not f.get("help")] == []


def test_every_advanced_field_sits_in_a_group_and_the_groups_come_in_order():
    advanced = [f for f in general.fields() if f.get("advanced")]
    assert all(f.get("group") for f in advanced)
    order = list(dict.fromkeys(f["group"] for f in advanced))
    assert order[:len(general.GROUPS)] == list(general.GROUPS)
    # each group's fields together, so the page draws one section per group
    seen: list[str] = []
    for f in advanced:
        if not seen or seen[-1] != f["group"]:
            assert f["group"] not in seen, f"{f['key']} is apart from its group"
            seen.append(f["group"])
    assert "routine_catch_up_days" not in {f["key"] for f in advanced}


@pytest.fixture
async def api(tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    app.include_router(appearance_api.router)
    async with client(app) as c:
        c.cfg = cfg
        yield c



@pytest.mark.anyio
async def test_the_routes_carry_the_help(api):
    roles = (await api.get("/api/roles")).json()["roles"]
    assert roles["chat"]["help"] == registry.ROLES["chat"]["help"]
    kinds = (await api.get("/api/providers")).json()["kinds"]
    openrouter = next(k for k in kinds if k["kind"] == "openrouter")
    sort = next(o for o in openrouter["options"] if o["key"] == "sort")
    assert sort["label"] == "Sort by" and sort["help"] == registry.OPTION_WORDS["sort"]["help"]
    assert ["price", "Price"] in sort["choices"]
    assert (await api.get("/api/appearance")).json()["help"] == appearance.HELP
    assert (await api.get("/api/agent")).json()["voice_help"] == voice.HELP
    catalog = (await api.get("/api/features")).json()["catalog"]
    web = next(f for f in catalog if f["id"] == "web")
    assert web["help"] == f"{web['about']} {web['off_when']}"


def test_an_option_without_words_is_its_key_in_words():
    v = registry.option_view("thinking_levels", "list", [], None)
    assert v["label"] == "Thinking levels" and v["help"] is None and v["choices"] is None


def test_the_cloud_help_is_filled_with_the_name():
    filled = registry.option_view("cloud", "bool", False, None,
                                  lambda t: t.replace("{{name}}", "Tink"))
    assert "Tink asks you before sending" in filled["help"] and "{{" not in filled["help"]


PRONOUN_PLACEHOLDER = re.compile(r"\{\{(?:she|her|herself|them)\}\}", re.IGNORECASE)


def test_a_they_agents_texts_name_it_and_no_text_holds_a_pronoun_placeholder():
    """The texts the app writes about the agent name it: filled for a They agent, none keeps
    a {{placeholder}}. No shipped text or prompt holds a pronoun placeholder, and
    {{pronouns}} fills with the agent's own."""
    from pathlib import Path

    from celine import approvals
    from celine.config import CONFIG_DIR
    from celine.guard import banners, bastet, view
    from celine.settings import tools_meta
    cfg = boot.load()
    cfg.agent.name, cfg.agent.pronouns = "Tink", "they"
    texts = [t for _, t in _texts()]
    texts += [m["summary"] for m in (*tools_meta.SKILLS.values(), *tools_meta.TOOLS.values())]
    texts += [*approvals.HELP.values(), *view.PAGE_HELP.values(), *banners.SAID.values(),
              banners.STILL_FROZEN, *bastet.DONE.values(), bastet.FROZEN_BY_YOU]
    for text in texts:
        filled = cfg.substitute(text)
        assert not re.search(r"\{\{\w+\}\}", filled), filled
        assert not re.search(r"\b(?:she|her|hers|herself)\b", filled, re.I), filled
    package = Path(approvals.__file__).parent
    shipped = [p for p in (*package.rglob("*.py"), *package.rglob("*.md"),
                           *CONFIG_DIR.rglob("*.md"), *CONFIG_DIR.rglob("*.toml"),
                           *CONFIG_DIR.rglob("*.example"))
               if p.name != "0026_agent_word.py" and "static" not in p.parts]
    assert len(shipped) > 100
    for p in shipped:
        assert not PRONOUN_PLACEHOLDER.search(p.read_text()), p
    assert cfg.substitute("{{name}} uses the pronouns {{pronouns}}.") == \
        "Tink uses the pronouns they/them."
