"""Which steps in flight may run again when a turn is picked up again."""
from __future__ import annotations

from collections.abc import Callable

# (wire name) -> (mutating, mode) from the live registry, or None when unknown
Flags = Callable[[str], "tuple[bool, str] | None"]


def safe_to_rerun(state: str, mutating, mode, flags: tuple[bool, str] | None) -> bool:
    """A step in flight may run again only when it is known to be read-only and
    blocking: re-running a search is harmless, re-running a side effect is not. What the
    call row recorded wins; the registry fills in a row recorded without it (a call to
    a tool that was not registered then)."""
    if state == "detached":                # its job died with the process
        return False
    if mutating is None or mode is None:
        if flags is None:
            return False
        mutating, mode = flags
    return not bool(mutating) and (mode or "blocking") == "blocking"


def restarted(rows, flags: Flags | None) -> str | None:
    """The sentence for the steps in flight (`rows`: name, state, mutating, mode) that may
    not run again; None when every one may (safe_to_rerun)."""
    unsafe = sorted({r["name"] for r in rows if not safe_to_rerun(
        r["state"], r["mutating"], r["mode"], flags(r["name"]) if flags else None)})
    if not unsafe:
        return None
    return (f"The server restarted while {', '.join(unsafe)} was running. The app does "
            f"not know if it finished.")
