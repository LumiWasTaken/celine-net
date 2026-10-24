"""A model for tests: replays a script of BackendEvents. Without a script it answers
"You said: <the last user text>" (`echo=True`) or a fixed reply."""
from __future__ import annotations

import anyio

from celine.backends.base import BackendEvent, Done, TextDelta

REPLY = "A reply from the test model."


def _text(content) -> str:
    """A message's words: a list of parts gives its text parts, never the list."""
    if isinstance(content, str):
        return content
    return " ".join(p.get("text", "") for p in content or [] if isinstance(p, dict)
                    and p.get("type") == "text")


class FakeBackend:
    name = "fake"
    supports_tools = True
    supports_images = True

    def __init__(self, script: list[BackendEvent] | None = None,
                 *, delay: float = 0.0, model: str = "fake-1", echo: bool = False):
        self.model = model
        self.script = script
        self.delay = delay
        self.echo = echo
        self.calls: list[list[dict]] = []

    async def stream(self, messages, tools, body=None, model=None):
        self.calls.append(messages)
        script = self.script
        if script is None:
            last = next((_text(m["content"]) for m in reversed(messages)
                         if m.get("role") == "user"), "")
            script = [TextDelta(f"You said: {last}" if self.echo else REPLY),
                      Done(reason="stop")]
        for ev in script:
            if self.delay:
                await anyio.sleep(self.delay)
            yield ev
