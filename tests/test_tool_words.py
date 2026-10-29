"""A tool call's row in words (skills/words.py): the same sentence the row has always
said, at work, done, failed and not run."""
import json

from celine.skills.words import forming, step, words


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
        == "main ls -la"
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
