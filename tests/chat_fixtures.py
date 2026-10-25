"""Helpers for tests that talk to a conversation: the user's message and upload,
scripted models, and a wait for a condition."""
from __future__ import annotations

import base64
import inspect

import anyio

from celine.backends.base import Done, TextDelta, ToolCallDelta
from fake_backend import FakeBackend


async def user_says(store, cid: str, text: str, attachment_ids=None, **kw):
    """The user's message to a conversation, as the composer sends it (an intercom
    message from `user`). -> (message_id, turn_id, seq), None for those while a turn
    of the conversation is live."""
    from celine.intercom import send
    out = await send(store, "user", f"agent:{cid}", text, attachments=attachment_ids, **kw)
    return out["message_id"], out["turn_id"], out["seq"]


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


async def upload(cfg, store, data: bytes, filename: str, content_type: str | None):
    """As an upload arrives: attachments.save reads a FastAPI UploadFile."""
    import io

    from starlette.datastructures import Headers, UploadFile

    from celine import attachments
    headers = Headers({"content-type": content_type}) if content_type else None
    return await attachments.save(cfg, store, UploadFile(io.BytesIO(data), filename=filename,
                                                         headers=headers))


async def until(check, seconds: float = 20):
    """Waits until check() is true; check may be sync or async."""
    with anyio.fail_after(seconds):
        while not ((await r) if inspect.isawaitable(r := check()) else r):
            await anyio.sleep(0.02)


def until_sync(check, seconds: float = 20):
    """`until` for a test without an event loop: waits on a worker thread's state.
    -> check()'s first true value."""
    import time
    end = time.monotonic() + seconds
    while not (r := check()):
        if time.monotonic() > end:
            raise TimeoutError(f"still not true after {seconds} s")
        time.sleep(0.02)
    return r


class Script(FakeBackend):
    """Replies in order; each reply is a list of events. Records whether each request
    carried tools."""

    def __init__(self, replies):
        super().__init__()
        self.replies = replies
        self.tools: list[bool] = []

    async def stream(self, messages, tools):
        self.calls.append(messages)
        self.tools.append(bool(tools))
        for ev in self.replies[min(len(self.calls), len(self.replies)) - 1]:
            yield ev


class Scripted(FakeBackend):
    """Calls `tools` in order, one per reply, then answers; what it calls next depends
    only on how many tool results it has seen (a resumed turn sends the journalled ones)."""

    def __init__(self, tools: list[str], crash_on_reply: int | None = None):
        super().__init__()
        self.tools, self.crash_on_reply, self.replies = tools, crash_on_reply, 0

    def stream(self, messages, tools):
        self.calls.append(messages)
        self.replies += 1
        if self.crash_on_reply == self.replies:
            # as the request goes out, outside the adapter's events (an error in them
            # fails the turn, turns/runner.py): nothing in the turn catches it
            raise RuntimeError("process died here")
        return self._reply(messages)

    async def _reply(self, messages):
        seen = sum(1 for m in messages if m.get("role") == "tool")
        if seen < len(self.tools):
            name, args = (self.tools[seen] if isinstance(self.tools[seen], tuple)
                          else (self.tools[seen], "{}"))
            yield ToolCallDelta(index=0, id=f"w{seen}", name=name, arguments=args)
            yield Done(reason="tool_calls")
        else:
            yield TextDelta("the answer")
            yield Done(reason="stop")
