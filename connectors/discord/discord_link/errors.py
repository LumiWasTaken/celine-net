"""How work ends, and the context a job runs with."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class Cancelled(Exception):
    """The job was cancelled. What was journaled stays."""


class Failed(Exception):
    """A failure whose message is one plain sentence for the user."""


class Unreachable(Exception):
    """Discord did not answer. Not a failure: the next run continues from the cursor."""


class DiscordError(Failed):
    """Discord answered, and the answer is a problem (login, rate limit)."""


class WriteRefused(Exception):
    """A write outside the one allowed chat. Raised before any socket is opened."""


@dataclass
class Ctx:
    progress: Callable[[int, int | None, str], None]
    cancelled: Callable[[], bool]

    def check(self) -> None:
        if self.cancelled():
            raise Cancelled()


NO_CTX = Ctx(progress=lambda *a: None, cancelled=lambda: False)
