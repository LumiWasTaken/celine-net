"""A conversation's finished exchanges, an archive tool with long results, and a model
that also writes the summaries, for the compaction and /compact tests."""
from __future__ import annotations

import json

from chat_fixtures import user_says

from celine.backends.base import BackendError, Done, TextDelta, ToolCallDelta
from fake_backend import FakeBackend
from celine.conversations import tree
from celine.skills import Registry, ToolResult, tool
from celine.turns import store as turns

SUMMARY = "SUMMARY: sam asked about trips; found [wa:1001] and [wa:1002]."


def archive_text(n_lines: int, start: int = 1000) -> str:
    """Looks like a find/read result: refs, dates, names, mixed language."""
    return "scope: all of Messages\n" + "\n".join(
        f"[wa:{start + i}] 2026-03-{1 + i % 28:02d} 12:{i % 60:02d} Alex: "
        f"wir fahren am Wochenende an die See, hast du Zeit? line {i}"
        for i in range(n_lines))


class Model(FakeBackend):
    """Answers summary requests with `summary` (None: the summariser is down); otherwise
    calls `tools` in order (one per reply), then answers. `overflow` raises the
    llama.cpp overflow error for the next N chat requests."""

    def __init__(self, tools=(), answer="ok", *, window=20_000, overflow=0,
                 summary=SUMMARY):
        super().__init__()
        self.tools, self.answer, self.context_window = list(tools), answer, window
        self.overflow, self.summary = overflow, summary
        self.summaries: list[list[dict]] = []

    async def stream(self, messages, tools):
        if messages and messages[0]["role"] == "system" and \
                messages[0]["content"].startswith("You write the memory"):
            self.summaries.append(messages)
            if self.summary is None:
                raise BackendError("summary backend down", retryable=True)
            yield TextDelta(self.summary)
            yield Done(reason="stop")
            return
        self.calls.append(messages)
        if self.overflow:
            self.overflow -= 1
            e = BackendError("This turn needs more context than fake has.", retryable=False)
            e.code, e.n_ctx, e.n_prompt_tokens = ("context_overflow", self.context_window,
                                                  self.context_window + 1)
            raise e
        last_user = max(i for i, m in enumerate(messages) if m.get("role") == "user")
        seen = sum(1 for m in messages[last_user + 1:] if m.get("role") == "tool")
        if seen < len(self.tools):
            yield ToolCallDelta(index=0, id=f"w{seen}", name=self.tools[seen],
                                arguments=json.dumps({"n": seen}))
            yield Done(reason="tool_calls")
        else:
            yield TextDelta(self.answer)
            yield Done(reason="stop")


class Arch:
    name = "arch"
    description = "test archive"

    def __init__(self, lines=300):
        self.lines, self.n = lines, 0

    @tool()
    async def find(self, ctx, n: int = 0) -> ToolResult:
        """Finds."""
        self.n += 1
        return ToolResult(content=archive_text(self.lines, start=5000 + self.n * 1000))


def arch_registry(arch=None):
    reg = Registry()
    reg.add(arch or Arch())
    return reg


async def seed_exchange(store, cid, q, results, answer="answered"):
    """One finished exchange: the question, one reply with calls, their results, the
    answer. results: [(tool name, content)]."""
    await user_says(store, cid, q)
    turn = await turns.claim(store, "w")
    tid = turn["id"]
    mid = await tree.start_assistant_message(store, cid, turn["root_message_id"], tid)
    await tree.finish_assistant_message(store, cid, mid, "")
    calls = [{"id": f"{tid}:0:{i}", "name": n, "arguments": json.dumps({"q": q}),
              "mutating": False, "mode": "blocking"} for i, (n, _) in enumerate(results)]
    if calls:
        await turns.record_tool_calls(store, cid, mid, calls)
        for c, (n, content) in zip(calls, results):
            await turns.finish_tool_call(store, cid, mid, c["id"], n, ToolResult(content=content),
                                         "ok", None)
    leaf = await tree.active_leaf(store, cid)
    a = await tree.start_assistant_message(store, cid, leaf, tid)
    await tree.finish_assistant_message(store, cid, a, answer)
    await turns.transition(store, tid, cid, "done")
    return tid, mid
