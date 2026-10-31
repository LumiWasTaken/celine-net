"""A tool call's row in words (skills/words.py): the same sentence the row has always
said, at work, done, failed and not run."""
import json
import sqlite3
import tomllib
from pathlib import Path
from types import SimpleNamespace


from celine.mcp import read_servers, row_words
from celine.skills import words as words_mod
from celine.skills.words import VERBS, fill, forming, group, runs, step, words


def w(name, args, state, display=None, result=None, own=None):
    return words(name, json.dumps(args), state, display, result, own)


def test_done_running_and_failed():
    sent = w("shell__send", {"path": ["/home/me/funnytime.pdf"]}, "ok")
    assert sent["settled"] == "Sent you funnytime.pdf"
    assert sent["running"] == "Sending you funnytime.pdf"
    assert w("notebook__read", {"path": "ml/muon.md"}, "error")["settled"] == \
        "Could not read the note ml/muon.md"
    assert w("plan__set", {"steps": [1, 2, 3]}, "ok")["settled"] == "Set a plan of 3 steps"
    assert w("email__read", {}, "ok", None, 'From: "Ann Lee" <ann@x.org>\nHi')["settled"] == \
        "Read the email from Ann Lee"
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


# Config.tool_words as boot.load makes it of the Mac plugin alone
OWN = row_words(SimpleNamespace(mcp={}), [SimpleNamespace(manifest=MAC)])


def m(name, args, state, display=None):
    return w(name, args, state, display, None, OWN)


def g(name, args):
    return group(name, args, OWN)


def test_a_servers_own_words():
    cmd = {"command": "brew install ffmpeg\nffmpeg -version"}
    run = m("macshell__start_process", cmd, "ok")
    assert run["settled"] == "Ran brew install ffmpeg on the Mac"
    assert run["running"] == "Running brew install ffmpeg on the Mac"
    assert g("macshell__start_process", cmd) == "Used the shell on the Mac {n} times"
    assert m("macshell__start_process", cmd, "error")["settled"] == \
        "Could not run brew install ffmpeg on the Mac"
    assert m("macshell__read_process_output", {"pid": 4021}, "ok")["settled"] == \
        "Read the output of process 4021 on the Mac"
    assert g("macshell__read_process_output", {"pid": 4021}) == g("macshell__start_process", cmd)
    # a missing argument: its default, or the hole and its quotes leave
    assert step("macshell__start_process", OWN) == "Running a command on the Mac"
    assert m("macshell__interact_with_process", {"pid": 7}, "ok")["settled"] == \
        "Typed in process 7 on the Mac"
    assert m("macscreen__type", {"text": "hello"}, "ok")["settled"] == 'Typed "hello" on the Mac'
    assert m("macscreen__click", {}, "ok")["settled"] == "Clicked on the Mac"
    # the same file read on either server is one kind of call
    assert g("macfiles__read_text_file", {"path": "/a.txt"}) == \
        g("macshell__read_file", {"path": "/b.txt"}) == "Read {n} files on the Mac"
    assert m("macbrowser__browser_navigate", {"url": "https://example.org"}, "ok")["settled"] \
        == "Opened https://example.org in Chrome on the Mac"
    assert forming("macshell__start_process", '{"command": "make test", "ti', OWN) == \
        "Running make test on the Mac"
    # a tool the server gave no words, and calls that stand alone
    assert m("macshell__give_feedback", {}, "ok")["settled"] == "Used macshell give feedback"
    assert g("macshell__give_feedback", {}) is None
    assert g("notebook__read", {"path": "a.md"}) is None
    # the box's run: its terminal from the arguments, the same at its start and after it
    assert g("shell__run", {"command": "ls", "terminal": "main"}) == \
        "Ran {n} commands in terminal main"


def test_fill():
    assert fill("Read {paths} on the Mac", {"paths": ["/a", "/b"]}) == "Read /a, /b on the Mac"
    assert fill("Read {p|a file}", {"p": {"x": 1}}) == "Read a file"         # a dict says nothing
    assert fill("Stopped process {pid} now", {"pid": 7}) == "Stopped process 7 now"
    assert fill("Waited {s} seconds", {"s": 2.0}) == "Waited 2 seconds"
    assert fill("Read {path|a file} here", {"path": ""}) == "Read a file here"
    assert fill('Typed "{text}" on the Mac', {}) == "Typed on the Mac"
    assert fill('Typed "{text}"', {"text": "  "}) == "Typed"
    assert fill("{text} ran", {}) == "ran"
    assert fill("Ran {cmd}", {"cmd": "x" * 70}) == f"Ran {'x' * 59}…"
    assert fill("Ran {cmd}", {"cmd": "\n  make  test\nmore"}) == "Ran make  test"   # its spaces stay
    assert fill("Ran {cmd|a command}", {}) == "Ran a command"


def test_row_words_of_an_mcp_toml_server():
    problems = []
    servers = read_servers("mcp.toml", {"servers": {
        "gh": {"enabled": True, "words": {"list_issues": "Listed the issues of {repo}",
                                          "bad": 3},
               "groups": {"list_issues": "Listed issues {n} times"}},
        "broken": {"enabled": True, "words": "Listed"}}}, problems, {})
    assert [p.message for p in problems] == \
        ["[servers.broken] words must be a table, not str; the default is used"]
    tables = row_words(SimpleNamespace(mcp=servers), [])
    assert tables["gh"] == {"words": {"list_issues": "Listed the issues of {repo}"},
                            "groups": {"list_issues": "Listed issues {n} times"}}
    assert tables["broken"] == {"words": {}, "groups": {}}


def call(i, name="shell__run", state="ok", exit=None, time_up=None, terminal="main"):
    return {"id": f"c{i}", "name": name, "state": state, "exit": exit, "time_up": time_up,
            "arguments": json.dumps({"command": f"ls {i}", "terminal": terminal})}


def test_runs():
    # three in a row are a run; two are not; the count and the failed ones from the server
    block = [call(1), call(2, exit=2), call(3, state="error"),
             call(4, terminal="other"), call(5, terminal="other")]
    assert runs([block]) == [{"ids": ["c1", "c2", "c3"],
                              "words": "Ran 3 commands in terminal main", "failed": 2}]
    # plumbing between them breaks nothing
    plumbing = {"id": "p", "name": "skills__read_guide", "state": "ok", "arguments": "{}"}
    assert runs([[call(1), plumbing, call(2), call(3)]])[0]["ids"] == ["c1", "c2", "c3"]
    # a reply's text between them ends the run
    assert runs([[call(1), call(2)], [call(3)]]) == []


def test_calls_that_stand_alone():
    for alone in (call(2, state="pending"), call(2, state="running"),
                  call(2, state="detached"), call(2, state="gated"),
                  call(2, state="error", time_up=1)):
        assert runs([[call(1), alone, call(3), call(4)]]) == [], alone["state"]
    # it joins its run once it ran
    assert runs([[call(1), call(2), call(3), call(4)]])[0]["words"] == \
        "Ran 4 commands in terminal main"


def test_runs_of_a_turn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE messages(id TEXT, turn_id TEXT, role TEXT, content TEXT, created_at TEXT);"
        "CREATE TABLE tool_calls(id TEXT, message_id TEXT, name TEXT, arguments TEXT, "
        "state TEXT, display TEXT);")
    def message(mid, content, *calls):
        c.execute("INSERT INTO messages VALUES(?,?,?,?,?)",
                  (mid, "t1", "assistant", content, f"2026-10-30T10:00:0{mid[1:]}"))
        for x in calls:
            c.execute("INSERT INTO tool_calls VALUES(?,?,?,?,?,?)",
                      (x["id"], mid, x["name"], x["arguments"], x["state"],
                       json.dumps({"exit": x["exit"]})))
    # each round of calls a message of its own; a text before a round ends the run
    message("m1", "Looking.", call(1))
    message("m2", "", call(2))
    message("m3", "", call(3), call(4))
    message("m4", "Now the rest.", call(5))
    message("m5", "", call(6))
    assert words_mod.runs_of_turn(c, "t1") == [
        {"ids": ["c1", "c2", "c3", "c4"], "words": "Ran 4 commands in terminal main",
         "failed": 0}]
    # an event says the runs only when the calls that settled changed them
    assert words_mod.runs_after(c, ["c6"]) is None
    c.execute("UPDATE tool_calls SET state='ok', display=? WHERE id='c4'", ('{"exit": 1}',))
    assert words_mod.runs_after(c, ["c4"]) == {"turn": "t1", "list": [
        {"ids": ["c1", "c2", "c3", "c4"], "words": "Ran 4 commands in terminal main",
         "failed": 1}]}
    # a call at work again: the runs go with the event, whatever they are
    c.execute("UPDATE tool_calls SET state='pending' WHERE id='c3'")
    assert words_mod.runs_after(c, ["c3"], reset=True) == {"turn": "t1", "list": []}


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
