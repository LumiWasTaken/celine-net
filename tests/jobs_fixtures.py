"""A detach tool and a scripted model for the background-job tests, and their cfg: a
short background window and short tool and job timeouts."""
from __future__ import annotations

import json

import anyio
import pytest

from chat_fixtures import until

from celine.backends.base import Done, TextDelta, ToolCallDelta
from fake_backend import FakeBackend
from celine.boot import load
from celine.jobs import store as job_rows
from celine.skills import ToolResult, tool


@pytest.fixture
def cfg(monkeypatch):
    from celine.turns import tools
    monkeypatch.setattr(tools, "BACKGROUND_AFTER", 0.15)
    c = load()
    c.settings.general.update({"tool_timeout_seconds": 0.4, "job_timeout_seconds": 5})
    return c


IMAGE = {"image": {"id": "att1", "sha256": "a" * 64, "filename": "anima-7.png",
                   "media_type": "image/png", "bytes": 1234, "width": 832,
                   "height": 1216},
         "preset": "Anima turbo", "seed": 7, "seconds": 52.0}


class Painter:
    """A detach tool asked into the background: a line before the window ends, one
    after it became a job, then it waits for the test to let it finish."""

    name = "painter"
    description = "paints"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.promoted = anyio.Event()
        self.release = anyio.Event()

    @tool(mode="detach", background="background")
    async def draw(self, ctx, background: bool = False) -> ToolResult:
        """Draws."""
        await ctx.emit("Waiting for the GPU")
        # inside the job nursery the context's turn_id is the job id
        await until(lambda: job_rows.get(ctx.store, ctx.turn_id), 3)
        await ctx.emit("Generating the image")
        self.promoted.set()
        await self.release.wait()
        if self.fail:
            return ToolResult(content="ComfyUI is offline", is_error=True)
        return ToolResult(content="Made one image. It is attachment att1.", display=IMAGE)


BG = json.dumps({"background": True})


def two_shot(tool_name: str, arguments: str = "{}"):
    class B(FakeBackend):
        async def stream(self, messages, tools):
            self.calls.append(messages)
            last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"),
                            default=-1)
            if not any(m.get("role") == "tool" for m in messages[last_user + 1:]):
                yield ToolCallDelta(index=0, id="p1", name=tool_name, arguments=arguments)
                yield Done(reason="tool_calls")
            else:
                yield TextDelta("all done")
                yield Done(reason="stop")
    return B()


async def tool_texts(store, cid) -> list[str]:
    """The tool rows of a conversation, oldest first."""
    return await store.read(lambda c: [r["content"] for r in c.execute(
        "SELECT content FROM messages WHERE conversation_id=? AND role='tool' "
        "ORDER BY created_at", (cid,))])
