"""One verb table: every built-in tool's name is
`<skill>__<verb>` or `<skill>__<verb>_<noun>` with a verb from the table, and the tools
of one skill that share a verb page with the same argument names. MCP tools are named
by their servers and are not checked."""
from __future__ import annotations

import pytest

from celine.boot import load
from celine.core.services import Services
from celine.skills import build

VERBS = {"search", "read", "list", "get", "count", "look_up", "scroll", "write", "edit",
         "delete", "move", "mark", "start", "stop", "send", "cancel", "type", "open",
         "act", "look", "close", "find", "wait", "update", "generate", "read_guide",
         "get_current", "set", "step", "change", "create", "continue", "request", "run", "load", "unload",
         "rotate", "think", "feel", "ask",
         # a reference added from its box
         "add",
         # its words in its own voice
         "draft",
         # a bug report
         "file",
         # its coding agent's sessions
         "status", "result", "abort",
         # its shares (shares.py)
         "share", "unshare",
         # where it is (skills/me.py)
         "whoami"}
PAGING = {"part", "from", "count", "offset", "page", "from_line", "before", "after",
          "limit"}


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setenv("CELINE_SHELL_URL", "http://shellbox:8770")
    cfg = load()
    cfg.skills = {n: {**s, "enabled": True} for n, s in cfg.skills.items()
                  if isinstance(s, dict)}
    return build(Services(cfg=cfg))


def verb(name: str) -> str:
    """`look_up` and `read_guide` are verbs of two words; else the first word."""
    return next((v for v in sorted(VERBS, key=len, reverse=True)
                 if name == v or name.startswith(v + "_")), name.split("_")[0])


def test_every_tool_is_named_with_a_verb_from_the_table(registry):
    bad = sorted(t.wire_name for t in registry.tools.values()
                 if verb(t.name) not in VERBS)
    assert bad == []


def test_tools_of_one_skill_that_share_a_verb_page_alike(registry):
    paging: dict[tuple[str, str], set[frozenset[str]]] = {}
    for t in registry.tools.values():
        names = frozenset(set(t.schema.get("properties") or {}) & PAGING)
        if names:
            paging.setdefault((t.skill, verb(t.name)), set()).add(names)
    assert {k: v for k, v in paging.items() if len(v) > 1} == {}


def test_every_tool_has_its_own_sentence_in_the_chat(registry):
    """A row is a sentence: a built-in tool never falls back to
    the MCP line "Used <skill> <tool>" (skills/words.py)."""
    from celine.skills.words import SENTENCES, VERB
    bare = sorted(t.wire_name for t in registry.tools.values()
                  if t.wire_name not in VERB and t.name not in SENTENCES.get(t.skill, {}))
    assert bare == []
