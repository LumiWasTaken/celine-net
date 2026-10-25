"""What a wait for the user inside a turn needs of that turn (celine/approvals.py):
whether the user waits on it (their own turn, origin "user"), its wall clock and its
Stop. The runner sets it for the turn it runs; the tasks it starts (its tools, Bastet's
look at a call) see it. Outside a turn it is None."""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Turn:
    interactive: bool       # the user's own turn: a request waits for them, no time limit
    clock: Any              # its wall clock (an anyio.CancelScope), held while it waits
    token: Any              # its Stop (turns/cancel.Token)


TURN: contextvars.ContextVar[Turn | None] = contextvars.ContextVar("waiting_turn",
                                                                  default=None)
