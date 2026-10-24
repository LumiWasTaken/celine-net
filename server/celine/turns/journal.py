"""Whether an interrupted turn can be picked up again."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Verdict = Literal["resumable", "unknown"]

# (wire name) -> (mutating, mode) from the live registry, or None when unknown
Flags = Callable[[str], "tuple[bool, str] | None"]


@dataclass(frozen=True)
class Resumability:
    verdict: Verdict
    reason: str
    rerun: tuple[str, ...] = ()          # call ids in flight that are safe to run again

    @property
    def safe(self) -> bool:
        return self.verdict == "resumable"


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
    return (f"the server restarted while {', '.join(unsafe)} was running. The app does "
            f"not know if it finished.")


def classify(c: sqlite3.Connection, turn_id: str, flags: Flags | None = None) -> Resumability:
    """Whether live turn `turn_id` may run on after a restart."""
    rows = c.execute(
        "SELECT tc.id, tc.name, tc.state, tc.mutating, tc.mode FROM tool_calls tc "
        "JOIN messages m ON m.id = tc.message_id "
        "WHERE m.turn_id = ? AND tc.state IN ('pending','running','detached')",
        (turn_id,)).fetchall()
    if why := restarted(rows, flags):
        return Resumability("unknown", why)
    rerun = tuple(r["id"] for r in rows)

    settled = c.execute(
        "SELECT count(*) FROM tool_calls tc JOIN messages m ON m.id = tc.message_id "
        "WHERE m.turn_id = ? AND tc.state NOT IN ('pending','running')",
        (turn_id,)).fetchone()[0]
    reason = (f"{settled} completed step(s) on disk" if settled else "no steps had started")
    if rerun:
        reason += f"; {len(rerun)} read-only step(s) will run again"
    return Resumability("resumable", reason, rerun)
