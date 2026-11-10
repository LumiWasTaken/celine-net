"""Require approval moved from the guard to Settings, Tools: the `[confirm] tools` list of
settings/guard.toml (beside celine.db) leaves that file, and its entries go to
settings/approval.toml `tools`, one tool each. A skill's name becomes every tool of it
(the built-in tools the Tools page names, settings/tools_meta.py, and the tools of it this
install has called); "tool:arg" becomes the tool. `shell__share:public` and `me__edit` are
left out: a public share and an edit of the agent ask on their own.

The guard's rows of that rule keep how they ended, under the rule name `approval`; an
ask still open is closed (its request lived in memory). Run again, it writes nothing."""
from __future__ import annotations

import tomllib
from pathlib import Path

ASK_ON_THEIR_OWN = ("shell__share:public", "me__edit")


def _tools(entries: list, conn) -> list[str]:
    from celine.settings.tools_meta import TOOLS
    called = [r[0] for r in conn.execute("SELECT DISTINCT name FROM tool_calls")]
    out: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or entry.strip() in ASK_ON_THEIR_OWN:
            continue
        what = entry.strip().partition(":")[0]
        if "__" in what:
            found = [what]
        else:
            found = sorted({t for t in (*TOOLS, *called) if t.startswith(what + "__")})
        out += [t for t in found if t not in ASK_ON_THEIR_OWN and t not in out]
    return out


def _read(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return None


def up(conn, db_path) -> None:
    from celine.settings.files import write_toml
    root = Path(db_path).parent / "settings"
    guard = _read(root / "guard.toml")
    if guard is not None and "confirm" in guard:
        confirm = guard.pop("confirm")
        entries = confirm.get("tools") if isinstance(confirm, dict) else None
        tools = _tools(entries if isinstance(entries, list) else [], conn)
        if tools:
            had = (_read(root / "approval.toml") or {}).get("tools") or []
            write_toml(root / "approval.toml",
                       {"tools": [*had, *(t for t in tools if t not in had)]})
        write_toml(root / "guard.toml", guard)
    conn.execute("BEGIN")
    conn.execute("UPDATE guard_decisions SET decision='closed' WHERE decision='asked'")
    conn.execute("UPDATE guard_decisions SET rule='approval' WHERE rule='confirm'")
    conn.execute("UPDATE guard_passes SET rule='approval' WHERE rule='confirm'")
    conn.execute("COMMIT")
