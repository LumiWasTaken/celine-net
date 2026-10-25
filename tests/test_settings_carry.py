"""Values saved before v1.2 reach their new place: a skill's numbers from an older
config/skills.toml, a contrast above the cap."""
from __future__ import annotations

import tomllib

from celine.settings import appearance, files, seed


def test_an_older_skills_toml_moves_its_numbers_and_keeps_saved_ones(tmp_path):
    old = tmp_path / "skills.toml"
    old.write_text("[sub_agents]\nenabled = true\nmax_running = 1\n"
                   "[web]\nmax_bytes = 4194304\nresults = 3\n"
                   "[chat_archive]\nverify_citations = false\n[gone]\nx = 1\n")
    root = tmp_path / "settings"
    root.mkdir()
    files.write(root, "general", {"web": {"results": 9}})
    assert seed.skill_knobs(root, old) == ["sub_agents.max_running", "verify_citations",
                                           "web.page_max_mb"]
    got = tomllib.loads((root / "general.toml").read_text())
    assert got["sub_agents"] == {"max_running": 1}
    assert got["web"] == {"results": 9, "page_max_mb": 4}, "a saved one stays"
    assert got["verify_citations"] is False
    assert seed.skill_knobs(root, old) == [], "once"


def test_a_contrast_saved_above_the_cap_reads_as_the_cap(tmp_path):
    files.write(tmp_path, "appearance", {"contrast": 150, "tint": 50})
    assert appearance.read(tmp_path) == {"contrast": appearance.CONTRAST_MAX, "tint": 50}
