"""config/ and var/settings read for a stranger: a file that does not parse ends the
start with the file named (exit 2), an unknown table or key and a wrong type are
Problems that name the file and the key, and the default counts."""
from __future__ import annotations

import shutil

import pytest

from celine import boot
from celine import config as config_mod
from celine.comfyui import presets
from celine.skills import BUILTIN


@pytest.fixture
def own_config(tmp_path, monkeypatch):
    """A copy of config/ to break, and a data dir of its own."""
    copy = tmp_path / "config"
    shutil.copytree(config_mod.CONFIG_DIR, copy)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", copy)
    monkeypatch.setattr(presets, "CONFIG_DIR", copy)
    monkeypatch.setenv("CELINE_DATA_DIR", str(tmp_path / "var"))
    return copy


def _start(capsys) -> str:
    from celine.__main__ import _load_config
    with pytest.raises(SystemExit) as e:
        _load_config()
    assert e.value.code == 2
    return capsys.readouterr().err


def test_a_config_file_that_does_not_parse_ends_the_start_with_its_line(own_config, capsys):
    (own_config / "llm.toml").write_text("[queue]\nwait_seconds = \n")
    err = _start(capsys)
    assert err.startswith("celine: ") and "llm.toml: " in err
    assert "line 2" in err and "column" in err


def test_a_settings_file_that_does_not_parse_ends_the_start_too(own_config, capsys):
    settings = own_config.parent / "var" / "settings"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "general.toml").write_text("timezone = Europe/Berlin\n")
    assert "general.toml: " in _start(capsys)


def test_a_missing_optional_file_is_its_defaults(own_config):
    (own_config / "llm.toml").unlink()
    cfg = boot.load()
    assert cfg.llm == {} and cfg.problems == []


def _edit(path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))


def test_an_unknown_table_or_key_is_a_problem_with_file_and_key(own_config):
    with (own_config / "celine.toml").open("a") as f:
        f.write("\n[nonsense]\nlaser = true\n")
    _edit(own_config / "skills.toml", "[web]\nenabled = true\n",
          "[web]\nenabled = true\nresults = 8\n")
    said = {(p.file.rsplit("/", 1)[-1], p.message) for p in boot.load().problems}
    assert ("celine.toml", "unknown table [nonsense], ignored") in said
    assert ("skills.toml", "unknown key [web] results, ignored") in said, \
        "a skill's numbers are on Settings, Advanced"


def test_a_wrong_type_is_a_problem_and_the_default_counts(own_config):
    _edit(own_config / "celine.toml", "# port = 8766", 'port = "six"')
    cfg = boot.load()
    said = {p.message for p in cfg.problems}
    assert "[server] port must be a whole number, not str; the default is used" in said
    assert cfg.port == 8766


def test_the_skill_tables_are_the_built_in_skills_and_hold_enabled_only(own_config):
    cfg = boot.load()
    assert set(cfg.skills) == {n for n, _, _ in BUILTIN}
    assert all(set(t) == {"enabled"} for t in cfg.skills.values())
    assert cfg.problems == []
