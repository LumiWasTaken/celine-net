"""The one switch and the prompt files (guard/switch.py, guard/prompts.py): off means no
Bastet anywhere; an edit of a config/guard file applies at the next ask."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from celine import guard
from celine.config import CONFIG_DIR
from celine.guard import prompts, questions, rules, talk

pytestmark = pytest.mark.anyio


def _cfg(tmp_path, guard_mode="on"):
    return SimpleNamespace(data_dir=tmp_path, substitute=lambda t, **_: t,
                           settings=SimpleNamespace(general={"guard": guard_mode}, guard={},
                                                    features={"guard": guard_mode != "off"}))


def test_every_shipped_prompt_loads_without_its_header_and_fills():
    cfg = SimpleNamespace(substitute=lambda t, **_: t)
    for name in ("hijack", "question", "page", "patrol", "reader", "talk"):
        text = prompts.text(name)
        assert text and not text.startswith("<!--"), name
    q = prompts.questions()
    assert set(q) == {"ask", "after", "answer"}
    assert set(rules.ASKED) | {"injection"} <= set(q["ask"])
    state = {"intent": "Owner: hi", "call": {"tool": "shell__run", "what": "Run.",
                                            "arguments": {"command": "ls"}},
             "sink": "house", "facts": []}
    assert "shell__run" in questions.render(cfg, state)


def test_an_edit_of_a_guard_prompt_applies_at_once(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    own = tmp_path / "guard"
    own.mkdir(parents=True)
    monkeypatch.setattr(prompts, "DIR", own)
    (own / "page.md").write_text("<!-- mine -->\nMine: {source}\n{page}\n")
    assert questions.render(cfg, {"page": "p", "source": "s"}) == "Mine: s\np"
    (own / "page.md").write_text("Again: {source}\n{page}\n")
    st = (own / "page.md").stat()
    os.utime(own / "page.md", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert questions.render(cfg, {"page": "p", "source": "s"}) == "Again: s\np"
    assert (CONFIG_DIR / "guard" / "page.md").read_text().startswith("<!--")


async def test_off_means_no_bastet_on_the_intercom(tmp_path):
    put = []
    pool = SimpleNamespace(put=lambda *a, **k: put.append(a))
    app = SimpleNamespace(cfg=_cfg(tmp_path, "off"), dispatcher=SimpleNamespace(work=pool))
    assert not guard.enabled(app.cfg) and guard.mode(app.cfg) == "off"
    await talk.heard(app, 1, "c1")
    assert put == []
