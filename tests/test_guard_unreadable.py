"""A judge's answer nobody can read: On refuses the call (it raises, calls.py says Bastet
broke), Watch counts it as no, as before."""
import pytest

from celine.guard import judge, questions

pytestmark = pytest.mark.anyio


async def _said(*_a, **_k):
    return "sure thing", None, "m"


async def test_an_unreadable_question_answer_fails_in_on_and_is_a_no_in_watch(monkeypatch):
    monkeypatch.setattr(questions, "complete", _said)
    monkeypatch.setattr(questions, "render", lambda cfg, state: "")
    monkeypatch.setattr(questions, "evidence", lambda state: "")
    turn = {"id": "t", "conversation_id": "c"}
    with pytest.raises(ValueError):
        await questions.SmallRole(None, None, turn)({}, {"leak": "q"})
    got = await questions.SmallRole(None, None, turn, background=True)({}, {"leak": "q"})
    assert got.p == {"leak": 0.0}


async def test_an_unreadable_second_stage_fails_in_on_and_is_an_allow_in_watch(monkeypatch):
    calls = iter([("D", None, "m"), ("no json here", None, "m")] * 2)

    async def said(*_a, **_k):
        return next(calls)

    async def her(*_a):
        return ""
    monkeypatch.setattr(judge, "complete", said)
    monkeypatch.setattr(judge, "prompt", lambda *a: "")
    monkeypatch.setattr(judge.intent, "read", her)
    turn = {"id": "t", "conversation_id": "c"}
    with pytest.raises(ValueError):
        await judge.ask(None, None, None, None, turn, {}, None)
    got = await judge.ask(None, None, None, None, turn, {}, None, background=True)
    assert got.deny is False


def test_her_own_persona_and_routines_are_always_looked_at():
    from celine.guard import trigger
    assert trigger.always("me__edit", {}) and trigger.always("routines__write", {})


def test_a_brace_in_a_name_stays_as_written_in_every_guard_prompt():
    import string
    from types import SimpleNamespace
    from celine.config import Config
    from celine.guard import bastet, prompts
    cfg = SimpleNamespace(user="C{3}PO", name="R{2}", pronouns="she")
    cfg.substitute = lambda text, **kw: Config.substitute(cfg, text, **kw)
    assert bastet.fill(cfg, "{{username}} and {{name}} ran {tool}.", tool="x") == \
        "C{3}PO and R{2} ran x."
    for name in ("hijack", "page", "patrol", "question", "review", "reader"):
        text = cfg.substitute(prompts.text(name), braces=True)
        keys = {f for _, f, _, _ in string.Formatter().parse(text) if f}
        assert "C{3}PO" in text.format(**dict.fromkeys(keys, "")) or "{{username}}" not in \
            prompts.text(name), name
