"""The texts a model reads are package files beside the module that reads them
(celine/prompts.py): every one fills with what its reader passes and leaves no Problem,
and every skill guide parses (skills.check_guides)."""
from __future__ import annotations

from pathlib import Path

from celine import prompts
from celine.boot import load

PACKAGE = Path(prompts.__file__).parent
# what the reader passes to `fill`; a file not here takes no values
FILLS = {
    "turns/compact.md": {"words": 100},
    "skills/mind_appraisal.md": {"emotions": "pleasure, arousal"},
    "sub_agents/fragment.md": {"agent_name": "a", "parent": "b", "cap": "1,966",
                                "can": "read", "where": "here"},
    "schedules/final.md": {"note": ""},
    "schedules/note.md": {"path": "diary/today.md"},
}


def test_every_package_text_fills_without_a_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("CELINE_DATA_DIR", str(tmp_path / "var"))
    cfg = load()
    files = [p for p in PACKAGE.rglob("*.md") if "guides" not in p.parts and "static" not in p.parts]
    assert len(files) >= 10
    for p in files:
        rel = p.relative_to(PACKAGE).as_posix()
        out = prompts.fill(cfg, str(p.parent / "x.py"), p.name, **FILLS.get(rel, {}))
        assert "{{" not in out, rel
    assert [str(p) for p in cfg.problems if "nothing fills" in str(p)] == []


def test_fill_says_once_what_nothing_fills(tmp_path, monkeypatch):
    monkeypatch.setenv("CELINE_DATA_DIR", str(tmp_path / "var"))
    cfg = load()
    (tmp_path / "t.md").write_text("Summarise for {{username}} in {{words}} words, {{style}}.\n")
    prompts.text.cache_clear()
    assert prompts.fill(cfg, str(tmp_path / "m.py"), "t.md", words=100).startswith("Summarise for ")
    assert [str(p) for p in cfg.problems if "nothing fills" in str(p)] == \
        [f"{tmp_path.name}/t.md: nothing fills {{{{style}}}}"]
    prompts.fill(cfg, str(tmp_path / "m.py"), "t.md", words=1)
    assert len([p for p in cfg.problems if "nothing fills" in str(p)]) == 1, "said once"


def test_every_skill_guide_parses():
    import importlib
    from celine.skills import BUILTIN, GUIDES_DIR, Guide, check_guides
    for _, module, _ in BUILTIN:
        importlib.import_module(f".{module}", "celine.skills")
    assert check_guides() is None
    for p in GUIDES_DIR.glob("*.md"):
        assert Guide(p.stem).body, p.name
