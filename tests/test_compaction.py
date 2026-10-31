"""Context builder and compaction: old results are aged (never deleted), earlier
exchanges summarised, an overflow retried once, and no tool result ever parted from
its call. Fakes only: a scripted backend that also writes summaries."""
from __future__ import annotations

from chat_fixtures import user_says
from compaction_fixtures import (SUMMARY, Arch, Model, arch_registry, archive_text,
                                 seed_exchange)

import json

import pytest

from celine.conversations import store as conversations, tree, view
from celine.boot import load
from celine.core.bus import EventBus
from celine.turns import compact, context, tokens
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from celine.turns import store as turns
from celine.turns import compactions

pytestmark = pytest.mark.anyio

SMALL = context.Policy(age_at=0.99, compact_at=0.5, compact_to=0.3)


@pytest.fixture
def cfg():
    c = load()
    c.settings.general.update({"tool_timeout_seconds": 10, "turn_wall_clock_seconds": 60,
                 "max_concurrent_turns": 1, "history_messages": 200})
    return c


@pytest.fixture(autouse=True)
def _fresh():
    old = context.POLICY
    yield
    context.POLICY = old


async def long_conversation(store, n=6, lines=60):
    cid = await conversations.create(store)
    for i in range(n):
        await seed_exchange(store, cid, f"question {i}",
                            [("arch__find", archive_text(lines, start=1000 * (i + 1))),
                             ("notebook__read", "note " * 400)])
    await user_says(store, cid, "and now?")
    return cid


def paired(messages) -> list[str]:
    """Asserts every tool result follows the call it answers and every call has its
    result; returns the tool results."""
    want: set[str] = set()
    for m in messages:
        if m["role"] == "tool":
            assert m["tool_call_id"] in want, f"a result without its call: {m}"
            want.discard(m["tool_call_id"])
        else:
            assert not want, f"calls without results: {want}"
            want = {c["id"] for c in m.get("tool_calls") or []}
    assert not want, f"calls without results: {want}"
    return [m["content"] for m in messages if m["role"] == "tool"]


async def test_old_tool_results_are_aged_not_deleted(store, cfg):
    cid = await long_conversation(store)
    b = Model(window=20_000)
    built = await context.build(store, cfg, b, cid, system="sys", tools=[])
    texts = paired(built.messages)
    assert any(t.startswith(compact.STUB_HEAD) for t in texts), built.actions
    assert all(not t.startswith(compact.STUB_HEAD) for t in texts[-4:]), "recent: verbatim"
    assert all(t.startswith("note") for t in texts if "note note" in t), "notebook: never"
    full = await store.read(lambda c: [r[0] for r in c.execute(
        "SELECT content FROM messages WHERE role='tool' AND content LIKE 'scope:%'")])
    assert len(full) == 6 and all(len(t) > 5000 for t in full), "the stored rows stay whole"
    stub = next(t for t in texts if t.startswith(compact.STUB_HEAD))
    assert "[wa:" in stub, "an aged result keeps its refs"
    again = await context.build(store, cfg, b, cid, system="sys", tools=[])
    assert json.dumps(again.messages) == json.dumps(built.messages), "stable, cacheable"


async def test_the_dry_build_reports_aging_only_when_a_start_would_age(store, cfg):
    cid = await long_conversation(store)
    context.POLICY = context.Policy(min_edit_tokens=10**9)
    built = await context.build(store, cfg, Model(window=20_000), cid, system="sys",
                                tools=[], mode="dry")
    assert not any("shorten" in a for a in built.actions), built.actions


async def test_compaction_summarises_and_an_edit_above_the_cut_undoes_it(store, cfg):
    cid = await long_conversation(store, n=10)
    context.POLICY = SMALL
    b = Model(window=20_000)
    built = await context.build(store, cfg, b, cid, system="PERSONA", tools=[])
    paired(built.messages)
    sys = built.messages[0]["content"]
    assert sys.startswith("PERSONA") and SUMMARY in sys
    users = [m["content"] for m in built.messages if m["role"] == "user"]
    assert not any("question 0" in u for u in users) and users[-1].endswith("and now?")
    assert built.estimate <= 0.5 * 20_000
    assert (await view.snapshot(store, cid))["compactions"], "kept, with the full rows"
    await context.build(store, cfg, b, cid, system="PERSONA", tools=[])
    assert len(b.summaries) == 1, "reused, not written again"
    first = (await compactions.context_view(store, cid))["chain"][0]["id"]
    await turns.request_abort(store, cid)            # the tree is edited once no turn is live
    await tree.edit(store, cid, first, "new start")
    assert (await compactions.context_view(store, cid))["compactions"] == [], "not on this branch"
    built = await context.build(store, cfg, b, cid, system="S", tools=[])
    assert SUMMARY not in built.messages[0]["content"]


async def test_a_turn_sends_every_result_with_its_call_and_ages_its_own(store, cfg):
    cid = await conversations.create(store)
    await seed_exchange(store, cid, "first", [("arch__find", "small result [wa:1]")])
    await user_says(store, cid, "research this")
    turn = await turns.claim(store, "w")
    # each result ~5.5k tokens; 5 of them do not fit 20k without aging
    b = Model(["arch__find"] * 5, window=20_000)
    await run_turn(turn, store, cfg, b, EventBus(), Token(turn["id"]), arch_registry(Arch(300)), None)
    assert (await view.snapshot(store, cid))["turn"]["state"] == "done"
    assert "small result [wa:1]" in paired(b.calls[0])
    texts = paired(b.calls[-1])
    assert len(texts) == 6
    assert texts[1].startswith(compact.STUB_HEAD) and not texts[-1].startswith(compact.STUB_HEAD)
    assert tokens.counter("fake-1").request(b.calls[-1]) <= 0.9 * 20_000


async def test_overflow_emergency_pass_then_retry_once(store, cfg):
    cid = await long_conversation(store, n=4, lines=100)
    turn = await turns.claim(store, "w")
    b = Model(window=65_536, overflow=1)
    await run_turn(turn, store, cfg, b, EventBus(), Token(turn["id"]), arch_registry(), None)
    snap = await view.snapshot(store, cid)
    assert snap["turn"]["state"] == "done"
    assert [(m["role"], m["content"]) for m in snap["messages"][-2:]] == [
        ("user", "and now?"), ("assistant", "ok")], "the retry streamed into the same message"
    paired(b.calls[-1])
    assert SUMMARY in b.calls[-1][0]["content"]


def test_a_terminal_stub_keeps_its_end_and_a_page_its_title_and_start():
    out = "\n".join(["terminal main  ~/src  exit 1 in 3 s"] + [f"line {i}" for i in range(40)]
                    + ["error: the build failed"])
    stub = compact.stub("shell__run", '{"command": "make"}', out)
    assert "Began: terminal main ~/src exit 1 in 3 s\nEnded:\n" in stub, "the status line"
    end = stub.split("Ended:\n")[1].split("\nAbout ")[0].splitlines()
    assert end[-1] == "error: the build failed" and end[0] == "line 33" and len(end) == 8
    assert "line 0" not in stub
    page = compact.stub("browser__open", '{"url": "https://a.example"}',
                        'tab main  https://a.example/  "A shop"\nWelcome to the shop\n'
                        "- [1] link Sign in")
    assert 'at https://a.example/  "A shop"\nBegan: Welcome to the shop / - [1] link' in page
    assert compact.stub("arch__find", "{}", "a\nb\nc").count("Began: a / b") == 1


def test_a_summary_lists_the_refs_it_dropped():
    items = [{"role": "assistant", "content": "", "calls": [
        {"name": "arch__find", "result": "[wa:1] a\n[wa:2] b\n[wa:3] c"}]}]
    out = compact.with_dropped_refs("found [wa:2].", items)
    assert out.endswith("not mentioned above: [wa:1] [wa:3]")
    assert compact.with_dropped_refs("found [wa:1] [wa:2] [wa:3].", items).endswith("[wa:3].")
    # the next summary keeps the earlier list, whether or not the model copied it
    later = [{"role": "assistant", "content": "", "calls": [
        {"name": "arch__find", "result": "[wa:9] z"}]}]
    for said in ("found [wa:3].", f"found [wa:3].\n\n{out.splitlines()[-1]}"):
        again = compact.with_dropped_refs(said, later, out)
        assert again == "found [wa:3].\n\n" + compact.DROPPED_HEAD + "[wa:1] [wa:9]"


async def test_aged_results_keep_cited_lines_and_a_full_window_warns(store, cfg):
    cid = await long_conversation(store)
    await store.write(lambda c: c.execute(
        "UPDATE messages SET content='see [wa:1003]' WHERE role='assistant' "
        "AND content='answered' AND conversation_id=?", (cid,)))
    b = Model(window=20_000)
    built = await context.build(store, cfg, b, cid, system="sys", tools=[])
    texts = paired(built.messages)
    stubs = [t for t in texts if t.startswith(compact.STUB_HEAD)]
    assert any("Lines you cited, kept whole:\n[wa:1003]" in t for t in stubs), stubs[0]
    assert built.messages[0]["content"] == "sys", "the system prompt as given, no warning"


async def test_a_completion_past_its_time_is_a_failure_never_the_part_so_far():
    import anyio

    from celine.backends.base import BackendError, TextDelta

    class Slow:
        model = "slow-model"

        async def stream(self, messages, tools):
            yield TextDelta("half a ")
            await anyio.Event().wait()           # never ends: the limit does
            yield TextDelta("never")
    asked = [{"role": "user", "content": "go"}]
    assert await compact.complete(Slow(), asked, 0) is None
    with pytest.raises(BackendError, match="slow-model did not finish its reply within 0"):
        await compact.complete(Slow(), asked, 0, errors=True)


async def test_a_chat_past_history_messages_keeps_the_last_ones(store, cfg):
    """A chat cuts history_messages at the first user message past the limit, as it
    always did; the big steps are a scene's."""
    cfg.settings.general.update({"history_messages": 12})
    cid = await conversations.create(store)
    for i in range(12):
        await seed_exchange(store, cid, f"question {i}", [])
    await user_says(store, cid, "and now?")          # 25 items, 13 past the limit
    built = await context.build(store, cfg, Model(window=100_000), cid, system="s", tools=[])
    asked = [m["content"] for m in built.messages if m["role"] == "user"]
    assert "question 7" in asked[0] and "question 6" not in json.dumps(built.messages)


async def test_a_background_completion_does_not_count_its_wait_in_the_queue():
    """complete's limit runs while the model works on the request: a queued backend's
    wait for its slot (backends/llm.paused) holds it, as it holds a turn's clock."""
    import math
    from celine.backends.base import Done, TextDelta
    from celine.backends.llm import TOOL_CLOCKS, paused
    seen = []

    class Queued:
        model = "m"

        async def stream(self, messages, tools):
            with paused(None):                       # waiting for the slot
                seen.append([c.deadline for c in TOOL_CLOCKS.get()])
            seen.append([c.deadline for c in TOOL_CLOCKS.get()])
            yield TextDelta("a title")
            yield Done(reason="stop")
    assert await compact.complete(Queued(), [], 30) == "a title"
    assert seen[0] == [math.inf], "its limit holds while it waits"
    assert seen[1][0] != math.inf, "and runs again once the model works"
