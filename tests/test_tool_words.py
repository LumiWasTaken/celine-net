"""A tool call's row in words (skills/words.py): the same sentence the row has always
said, at work, done, failed and not run."""
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from celine.mcp import row_words
from celine.skills import words as words_mod
from celine.skills.words import VERBS, forming, step, words


def w(name, args, state, display=None, result=None):
    return words(name, json.dumps(args), state, display, result)


def test_done_running_and_failed():
    sent = w("shell__send", {"path": ["/home/me/funnytime.pdf"]}, "ok")
    assert sent["settled"] == "Sent you funnytime.pdf"
    assert sent["running"] == "Sending you funnytime.pdf"
    assert w("notebook__read", {"path": "ml/muon.md"}, "error")["settled"] == \
        "Could not read the note ml/muon.md"
    assert w("plan__set", {"steps": [1, 2, 3]}, "ok")["settled"] == "Set a plan of 3 steps"
    assert w("email__read", {}, "ok", None, 'From: "Ann Lee" <ann@x.org>\nHi')["settled"] == \
        "Read the mail from Ann Lee"
    long = "x" * 70
    assert w("web__search", {"query": long}, "ok")["settled"] == \
        f'Searched the web for "{"x" * 59}…"'


def test_scope_and_chips():
    d = {"scope_kind": "chats",
         "scope_chats": [{"ref": "whatsapp:1", "name": "Ann", "platform": "whatsapp"}]}
    out = w("chat_archive__search", {"words": ["tea", "cake"]}, "ok", d)
    assert out["settled"] == 'Searched Ann (WhatsApp) for "tea" and 1 more phrase'
    assert out["where"] == "Ann (WhatsApp)"
    assert w("chat_archive__read_session", {"ref": "whatsapp:9"}, "ok")["settled"] == \
        "Read the chat session around [[whatsapp:9]]"


def test_not_run_and_cut_off():
    gated = w("web__search", {"query": "tea"}, "gated", None,
              "Down: SearXNG does not answer. Start it.")
    assert gated["settled"] == 'Could not search the web for "tea": searXNG does not answer'
    late = w("notebook__search", {"query": "tea"}, "error", {"time_up": True},
             "Not run: the time for this turn is up.")
    assert late["settled"] == "Not run: the time for this turn is up"
    # still at work: its words if the turn cuts it off
    assert w("notebook__list", {}, "running")["settled"] == "Listed the notebook"


def test_rows_with_their_own_words():
    assert w("shell__run", {"command": "ls -la\npwd"}, "ok", {"terminal": "main"})["settled"] \
        == "Ran ls -la in terminal main"
    assert w("shell__run", {"command": "ls"}, "running", {"terminal": "lan"})["running"] \
        == "Running ls in terminal lan"
    assert w("shell__type", {"text": "y", "keys": ["Enter"], "terminal": "lan"}, "ok")["settled"] \
        == 'Typed "y" and pressed Enter in terminal lan'
    assert w("shell__read", {"terminal": "lan"}, "ok")["settled"] == "Read terminal lan"
    assert w("shell__read", {}, "running")["running"] == "Listing the terminals"
    assert w("missions__update", {"status": "needs_you", "note": "the key"}, "ok")["settled"] \
        == "Mission needs you: the key"
    assert w("image_generator__generate", {"items": [{"prompt": "a fox"}]}, "ok")["settled"] \
        == "Images a fox"
    assert w("browser__act", {"steps": [{"do": "type", "text": "Hedy"}, {"do": "key", "text": "Enter"}]},
             "ok", {"url": "https://www.wikipedia.org/x"})["settled"] == \
        'Typed "Hedy", then pressed Enter'


def test_mcp_tool_step_and_forming():
    assert w("github__list_issues", {"q": "bug"}, "ok")["settled"] == \
        'Used github list issues for "bug"'
    assert step("notebook__read") == "Reading the note"
    assert step("intercom__send") == "Sending a message"
    assert forming("notebook__read", '{"path": "ml/muon.md", "fr') == \
        "Reading the note ml/muon.md"


def test_a_command_is_its_first_line_cut():
    first = "for i in $(seq 1 60); do s=$(curl -s -m 5 http://192.168.1.30:8765/health); " \
            "[ -n \"$s\" ] && break; sleep 1; done"
    long = f"{first}\necho done"
    assert w("shell__run", {"command": long}, "ok", {"terminal": "main"})["settled"] == \
        f"Ran {first[:79]}… in terminal main"
    assert w("shell__run", {"command": long}, "error", {"terminal": "main"})["settled"] == \
        f"Could not run {first[:79]}… in terminal main"


MAC = tomllib.loads((Path(__file__).parents[1] / "plugins/mac/plugin.toml").read_text())


@pytest.fixture
def mac(monkeypatch):
    said, groups = row_words(SimpleNamespace(mcp={}), [SimpleNamespace(manifest=MAC)])
    monkeypatch.setattr(words_mod, "MCP_WORDS", said)
    monkeypatch.setattr(words_mod, "MCP_GROUPS", groups)


def test_a_servers_own_words(mac):
    cmd = {"command": "brew install ffmpeg\nffmpeg -version"}
    run = w("macshell__start_process", cmd, "ok")
    assert run["settled"] == "Ran brew install ffmpeg on the Mac"
    assert run["running"] == "Running brew install ffmpeg on the Mac"
    assert run["group"] == "Used the shell on the Mac {n} times"
    assert w("macshell__start_process", cmd, "error")["settled"] == \
        "Could not run brew install ffmpeg on the Mac"
    read = w("macshell__read_process_output", {"pid": 4021}, "ok")
    assert read["settled"] == "Read the output of process 4021 on the Mac"
    assert read["group"] == run["group"]
    # a missing argument: its default, or the hole and its quotes leave
    assert step("macshell__start_process") == "Running a command on the Mac"
    assert w("macshell__interact_with_process", {"pid": 7}, "ok")["settled"] == \
        "Typed in process 7 on the Mac"
    assert w("macscreen__type", {"text": "hello"}, "ok")["settled"] == 'Typed "hello" on the Mac'
    assert w("macscreen__click", {}, "ok")["settled"] == "Clicked on the Mac"
    # the same file read on either server is one kind of call
    assert w("macfiles__read_text_file", {"path": "/Users/c/a.txt"}, "ok")["group"] == \
        w("macshell__read_file", {"path": "/Users/c/b.txt"}, "ok")["group"] == \
        "Read {n} files on the Mac"
    assert w("macbrowser__browser_navigate", {"url": "https://example.org"}, "ok")["settled"] \
        == "Opened https://example.org in Chrome on the Mac"
    assert forming("macshell__start_process", '{"command": "make test", "ti') == \
        "Running make test on the Mac"
    # a tool the server gave no words, and calls that stand alone
    assert w("macshell__give_feedback", {}, "ok")["settled"] == "Used macshell give feedback"
    assert w("macshell__give_feedback", {}, "ok")["group"] is None
    assert w("notebook__read", {"path": "a.md"}, "ok")["group"] is None
    assert w("shell__run", {"command": "ls"}, "ok", {"terminal": "main"})["group"] == \
        "Ran {n} commands in terminal main"


# the tools and their arguments as the Mac's servers list them (the versions setup.sh pins)
MAC_TOOLS = json.loads((Path(__file__).parent / "data/mac_tools.json").read_text())


def test_every_mac_tool_has_words_and_a_group():
    assert set(MAC["servers"]) == set(MAC_TOOLS)
    for sid, server in MAC["servers"].items():
        tools = MAC_TOOLS[sid]
        assert set(server["words"]) == set(tools), sid
        assert set(server["groups"]) == set(tools), sid
        assert all("{n}" in g for g in server["groups"].values()), sid
        for tool, text in server["words"].items():
            assert text.partition(" ")[0] in VERBS, text
            # a hole names a real argument, or its subject silently drops
            for hole in words_mod._HOLE.finditer(text):
                assert hole.group(2) in tools[tool], f"{sid} {tool}: {{{hole.group(2)}}}"
