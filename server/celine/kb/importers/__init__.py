"""What every task that writes into a knowledge base shares: its context (progress,
cancel) and the three ways it ends early."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class Cancelled(Exception):
    """Raised inside an importer when the task was cancelled. What was written stays."""


class Failed(Exception):
    """A task failure whose message is already one plain sentence for the user; the
    task shows it as it is (no exception name in front)."""


class Unreachable(Exception):
    """The connection or server did not answer. Not a failure: the work continues from
    its cursor on the next run. The message is one plain sentence naming what did not
    answer ("Discord is not reachable.")."""


@dataclass
class ImportContext:
    progress: Callable[[int, int | None, str], None]
    cancelled: Callable[[], bool]

    def check(self) -> None:
        if self.cancelled():
            raise Cancelled()
