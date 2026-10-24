"""The merge gate's check: every kind of long text (texts.KINDS) has a place whose save
calls `texts.record` (or `texts.keep`), so its History fills. A place counts when one file both calls
record and names the kind (`"lore:` or `f"lore:`)."""
from __future__ import annotations

import re
from pathlib import Path

from celine import texts

SERVER = Path(texts.__file__).parent
OWN = {SERVER / "texts.py", SERVER / "api" / "texts.py"}
CALLS = re.compile(r"\btexts\.(?:record|keep)\(|from \.+texts import record\b")


# kinds whose save path is not built yet: lyrics (no large editor yet). A kind leaves the
# list once it is recorded, and no kind joins it.
LATER = {"lyrics"}


def test_every_kind_of_long_text_is_recorded_where_it_is_saved():
    savers = [p.read_text() for p in SERVER.rglob("*.py")
              if p not in OWN and CALLS.search(p.read_text())]
    recorded = {k for k in texts.KINDS
                if any(re.search(rf"""f?["']{k}:""", s) for s in savers)}
    assert not recorded & LATER, f"recorded now, take them off LATER: {recorded & LATER}"
    missing = set(texts.KINDS) - recorded - LATER
    assert not missing, f"no save path records these long texts: {sorted(missing)}"
