"""docs/plugins.md names every key a plugin.toml, a server block and a settings field type
take, so the guide grows with the code."""
from __future__ import annotations

from pathlib import Path

from celine import mcp, plugins
from celine.settings import form

GUIDE = (Path(__file__).parent.parent / "docs" / "plugins.md").read_text()


def test_the_plugin_guide_names_every_key_the_code_reads():
    keys = [*plugins.MANIFEST, *mcp.KEYS, *form.TYPES]
    assert [k for k in keys if f"`{k}`" not in GUIDE] == []
