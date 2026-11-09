"""Reading in parts (skills/parts.py): every part of a long text, read one by one in a
turn with a small output room, passes the result backstop whole, and the parts join to
the text."""
from __future__ import annotations

import re

from celine.skills import ToolResult
from celine.skills.parts import tail
from celine.turns.tools import _capped

ROOM = 2000     # the turn's output room (ToolContext.max_output_tokens) while reading


async def joined(read, call: str) -> tuple[str, int]:
    """(the parts joined, how many) of the text `read(part)` returns one part of, its
    tail lines naming `call`."""
    first = await read(1)
    n = int(re.search(r"^part 1 of (\d+)$", first, re.M)[1])
    got = []
    for k in range(1, n + 1):
        out = first if k == 1 else await read(k)
        assert _capped(ToolResult(content=out), {"max_output_tokens": ROOM}).content == out, \
            f"part {k} passes the result backstop whole"
        top = f"part {k} of {n}\n\n"
        assert top in out and "\n\n" + tail(k, n, call) in out, out[:300] + "..." + out[-300:]
        got.append(out[out.index(top) + len(top):out.rindex("\n\n" + tail(k, n, call))])
    return "".join(got), n
