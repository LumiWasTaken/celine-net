"""Where each skill is offered, as the Tools page names it (settings/tools_meta.py)."""
from __future__ import annotations

from celine.settings import tools_meta


def test_every_label_is_plain_words_from_the_words_list():
    """The Tools page shows no internal name: each skill's word is in docs/ui/words.md
    under Tools, and each tool's label is that word, a colon and plain words."""
    from pathlib import Path
    words = (Path(__file__).parent.parent / "docs" / "ui" / "words.md").read_text()
    table = words.split("### Tools\n", 1)[1].split("\n### ", 1)[0]
    approved = {w for line in table.splitlines() if line.startswith("| ")
                for w in line.split("|")[1].strip().split(", ")}
    for m in tools_meta.SKILLS.values():
        assert m["label"] in approved, m["label"]
    for wire, m in tools_meta.TOOLS.items():
        assert "_" not in m["label"] and m["label"].split(": ")[0] in approved, wire
