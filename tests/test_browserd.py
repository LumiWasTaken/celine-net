"""browserd's call handling with no browser: CDP is a recorder, PIL a stub when it is
not installed (only shots draw)."""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from celine.browser import sync
from celine.browser.sync import BrowserRefused
from celine.conversations import store as conversations
from celine.skills.browser import Browser
from celine.turns import loads
from vault_fixtures import by_hand, ctx_for, fake_bw, vault_app  # noqa: F401

ROOT = Path(__file__).parent.parent
try:
    import PIL  # noqa: F401
except ImportError:
    sys.modules["PIL"] = types.ModuleType("PIL")
    for name in ("Image", "ImageDraw", "ImageFont"):
        setattr(sys.modules["PIL"], name, None)
sys.path.insert(0, str(ROOT / "browser"))
spec = importlib.util.spec_from_file_location("browserd", ROOT / "browser" / "browserd.py")
browserd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(browserd)
sys.path.remove(str(ROOT / "browser"))


@pytest.fixture
def box(tmp_path, monkeypatch):
    sent = []
    answers = {}                      # method -> a function of the params, for one test

    def cdp(method, params=None, session=None, wait=0):
        sent.append((method, params or {}))
        if method in answers:
            return answers[method](params or {})
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method == "Target.createTarget":
            return {"targetId": "t9"}
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"pageX": 0, "pageY": 600, "clientHeight": 900},
                    "cssContentSize": {"height": 3000}}
        if method == "DOM.getContentQuads":
            return {"quads": [[0, 0, 100, 0, 100, 40, 0, 40]]}
        return {}

    monkeypatch.setattr(browserd, "cdp", cdp)
    for name in ("SCROLLED", "POLL"):
        monkeypatch.setattr(browserd, name, 0)
    monkeypatch.setattr(browserd, "EFFECT_WAIT", 0.05)
    monkeypatch.setattr(browserd, "STILL", 0.05)
    monkeypatch.setattr(browserd, "TABS_FILE", tmp_path / "tabs.json")
    monkeypatch.setattr(browserd, "DRAFTS_FILE", tmp_path / "drafts.json")
    monkeypatch.setitem(browserd.STATUS, "state", "up")
    monkeypatch.setitem(browserd.STATUS, "front", None)
    monkeypatch.setattr(browserd, "tabs", {})
    monkeypatch.setattr(browserd, "notes", [])
    monkeypatch.setattr(browserd, "spare", None)
    tab = browserd.tabs["a"] = browserd.Tab("a", "t1", "https://x/")
    return SimpleNamespace(tab=tab, sent=sent, answers=answers)


def test_a_bad_show_leaves_the_tab_free(box):
    with pytest.raises(browserd.Problem):
        browserd.do_call({"tab": "a", "steps": [{"do": "reload"}], "show": "most"})
    assert not box.tab.lock.locked()


def test_only_the_main_document_fails_the_page(box):
    on = browserd.on_event
    on("Network.requestWillBeSent", {"type": "Document", "frameId": "t1", "requestId": "r1"}, "s1")
    on("Network.loadingFailed", {"type": "Document", "requestId": "r2",
                                 "errorText": "net::ERR_BLOCKED_BY_CLIENT"}, "s1")
    assert box.tab.error is None, "a frame's document is not the page"
    on("Network.loadingFailed", {"type": "Document", "requestId": "r1",
                                 "errorText": "net::ERR_NAME_NOT_RESOLVED"}, "s1")
    assert box.tab.error == "net::ERR_NAME_NOT_RESOLVED"


def test_long_text_ends_with_enter(box):
    browserd.act(box.tab, {"do": "type", "text": "x" * 300 + "\n"}, {})
    inserted = [p for m, p in box.sent if m == "Input.insertText"]
    keys = [p["key"] for m, p in box.sent if m == "Input.dispatchKeyEvent"]
    assert inserted == [{"text": "x" * 300}] and keys == ["Enter", "Enter"]


def test_select_on_a_combobox_and_px_in_a_scroll_area(box):
    frame = SimpleNamespace(owner=None)
    box.tab.nodes = {7: browserd.Node(7, "s1", 70, "combobox", (0, 0, 100, 40), frame),
                     8: browserd.Node(8, "s1", 80, "scrollarea", (0, 0, 100, 40), frame)}
    with pytest.raises(browserd.Problem) as e:
        browserd.choose(box.tab, "e7", "Large")
    assert (e.value.code, e.value.facts) == ("no_select", {"role": "combobox"})
    browserd.scroll(box.tab, "e8", "300")
    [wheel] = [p for m, p in box.sent if p.get("type") == "mouseWheel"]
    assert wheel["deltaY"] == 300, "pixels are pixels, also inside a scroll area"


def test_a_look_that_reads_the_page_under_a_dialog_is_refused(box):
    box.tab.dialog = {"type": "alert", "message": "hi"}
    for body in ({"what": "page"}, {"what": "view", "find": "x"}, {"what": "shot"}):
        with pytest.raises(browserd.Problem) as e:
            browserd.run(box.tab, body)
        assert e.value.code == "dialog"


def test_the_strip_brings_a_tab_to_the_front_unless_another_is_in_a_call(box):
    b = browserd.tabs["b"] = browserd.Tab("b", "t2", "https://y/")
    assert browserd.control("front", "b") == {"front": "b"}
    assert browserd.STATUS["front"] == "b"
    assert box.sent[-1] == ("Page.bringToFront", {})
    box.tab.lock.acquire()
    with pytest.raises(browserd.Problem) as e:
        browserd.control("front", "b")
    assert (e.value.code, e.value.facts) == ("busy", {"tab": "a"})
    assert browserd.control("front", "a") == {"front": "a"}, "its own tab may come forward"
    box.tab.lock.release()
    with pytest.raises(browserd.Problem) as e:
        browserd.control("front", "zz")
    assert e.value.code == "no_tab"
    assert not b.lock.locked()


def test_the_strip_closes_an_idle_tab_and_never_a_busy_one(box):
    b = browserd.tabs["b"] = browserd.Tab("b", "t2", "https://y/")
    browserd.notes.append("download:cv.pdf")
    browserd.control("front", "b")
    b.lock.acquire()
    with pytest.raises(browserd.Problem) as e:
        browserd.control("close", "b")
    assert (e.value.code, e.value.facts) == ("busy", {"tab": "b"})
    assert "b" in browserd.tabs
    b.lock.release()
    assert browserd.control("close", "b") == {"front": "a"}, "the tab it used last comes forward"
    assert list(browserd.tabs) == ["a"] and not b.lock.locked()
    assert ("Target.closeTarget", {"targetId": "t2"}) in box.sent
    assert browserd.notes == ["download:cv.pdf"], "the notes wait for its next result, as on its close"


def ax(backend, role, name="", parent=None, **props):
    return {"nodeId": str(backend), "backendDOMNodeId": backend, "ignored": False,
            "role": {"value": role}, "name": {"value": name},
            **({"parentId": str(parent)} if parent else {}),
            "properties": [{"name": k, "value": {"value": v}} for k, v in props.items()]}


def test_a_click_is_refused_on_a_disabled_or_covered_target(box):
    """Before the click: disabled, then the hit test at the centre (50, 20): the target,
    a node inside it, or something else, named by its nearest landmark."""
    frame = SimpleNamespace(owner=None)
    box.tab.nodes = {1: browserd.Node(1, "s1", 10, "button", (0, 0, 100, 40), frame),
                     2: browserd.Node(2, "s1", 20, "button", (0, 0, 100, 40), frame)}
    tree = {10: [ax(10, "button", "Off", disabled=True)], 20: [ax(20, "button", "Go")],
            99: [ax(99, "StaticText", "We use cookies", 98), ax(98, "dialog", "Cookies", 1),
                 ax(1, "RootWebArea")]}
    box.answers["Accessibility.getPartialAXTree"] = lambda p: {"nodes": tree[p["backendNodeId"]]}
    hit = {"at": 21}
    # page coordinates: the view's centre of the box plus the 600 px the page is scrolled
    box.answers["DOM.getNodeForLocation"] = lambda p: {
        "backendNodeId": hit["at"] if (p["x"], p["y"]) == (50, 620) else 1}
    parents = {"s1": {21: 20, 20: 1, 99: 98, 98: 1}}
    with pytest.raises(browserd.Problem) as e:
        browserd.aim(box.tab, "e1", parents)
    assert e.value.code == "disabled"
    assert browserd.aim(box.tab, "e2", parents) == (50, 20, (0, 0, 100, 40)), "a node inside it"
    hit["at"] = 99
    with pytest.raises(browserd.Problem) as e:
        browserd.aim(box.tab, "e2", parents)
    assert (e.value.code, e.value.facts) == ("covered", {"by": {"role": "dialog", "name": "Cookies"}})
    assert not [m for m, _ in box.sent if m == "Input.dispatchMouseEvent"]


def test_an_outline_line_bare_of_ids_new_and_active():
    assert browserd.bare('- button "Agree" e2 [active, new]') == '- button "Agree"'
    assert browserd.bare('- checkbox "Bacon" e9 [checked, new]:') == '- checkbox "Bacon" [checked]:'
    assert browserd.bare("- text: a link[e14] inline") == "- text: a link inline"
    assert browserd.diff(["a", "b", "b"], ["b", "c", "c"]) == {"added": 2, "gone": 2, "lines": ["c", "c"]}


def test_the_skill_knows_the_box_s_numbers():
    """skills/browser.py repeats browserd's numbers; the app never imports the box."""
    from celine.skills import browser as skill
    assert (skill.CDP_PORT, skill.MAX_TABS, skill.STEPS_MAX) == (
        browserd.CDP_PORT, browserd.MAX_TABS, browserd.STEPS_MAX)


def test_each_step_has_its_effect(box, monkeypatch):
    """A swap of text in place is a change; the same outline is nothing; a navigation
    says where to."""
    pages = []
    monkeypatch.setattr(browserd, "fingerprint", lambda tab: {
        "navs": tab.navs, "loading": False, "downloads": 0, "focus": None, "parents": {},
        "lines": pages[0] if len(pages) == 1 else pages.pop(0)})
    pages[:] = [["- heading \"Question 1 of 46\""], ["- heading \"Question 2 of 46\""]]
    before = browserd.fingerprint(box.tab)
    assert browserd.effect(box.tab, before) == {
        "effect": "changed", "added": 1, "gone": 1, "lines": ['- heading "Question 2 of 46"']}
    pages[:] = [["- heading \"Question 2 of 46\""]]
    assert browserd.effect(box.tab, browserd.fingerprint(box.tab)) == {"effect": "none"}
    before = browserd.fingerprint(box.tab)
    box.tab.navs += 1
    box.answers["Target.getTargetInfo"] = lambda p: {"targetInfo": {"url": "https://x/next",
                                                                    "title": "Next"}}
    assert browserd.effect(box.tab, before) == {"effect": "navigated", "url": "https://x/next"}


def test_plain_text_into_a_password_field_is_refused_before_any_cdp_call(box):
    """By its id from its last outline, before anything runs; at the cursor, when the
    focused line is a password field, before a key is pressed. The text is in no fact."""
    frame = SimpleNamespace(owner=None)
    box.tab.nodes = {14: browserd.Node(14, "s1", 140, "textbox", (0, 0, 100, 40), frame,
                                       password=True)}
    n = len(box.sent)
    with pytest.raises(browserd.Problem) as e:
        browserd.do_call({"tab": "a", "steps": [{"do": "click", "target": "e3"},
                                                {"do": "type", "target": "e14",
                                                 "text": "plain-text-pass-1"}]})
    assert (e.value.code, e.value.facts) == ("password_field",
                                             {"target": "e14", "done": 0, "effects": []})
    assert box.sent[n:] == [] and not box.tab.lock.locked()
    with pytest.raises(browserd.Problem) as e:
        browserd.act(box.tab, {"do": "type", "text": "plain-text-pass-1"}, {},
                     focus='- password field "Password"')
    assert (e.value.code, e.value.facts) == ("password_field", {})
    assert not [m for m, _ in box.sent[n:] if m.startswith("Input.")]


def _login_page(box, monkeypatch, vault_app) -> SimpleNamespace:
    """The agent's browser on a sign-in page whose e14 is a password field, through browserd:
    .browser (the skill), .shots (the step shots taken), .page (its address) and .focus
    (the focused outline line, for a type at the cursor or at a point)."""
    frame = SimpleNamespace(owner=None)
    box.tab.nodes = {14: browserd.Node(14, "s1", 140, "textbox", (0, 0, 100, 40), frame,
                                       password=True)}
    page = SimpleNamespace(url="https://x/login", focus=None, shots=[], frame=None,
                           password=True)
    box.answers["Target.getTargetInfo"] = lambda p: {"targetInfo": {"url": page.url,
                                                                    "title": "Sign in"}}
    # browserd.on_its_site: the page's address, the cursor's frame's, and whether the
    # cursor is in an <input type=password>
    box.answers["Runtime.evaluate"] = lambda p: {"result": {"value": {
        "top": page.url, "frame": page.url if page.frame is None else page.frame,
        "password": page.password}}}
    box.answers["Page.navigate"] = lambda p: setattr(page, "url", p["url"]) or {}
    monkeypatch.setattr(browserd, "step_shot",
                        lambda tab, aimed: page.shots.append(aimed) or "")
    monkeypatch.setattr(browserd, "fingerprint", lambda tab: {
        "navs": tab.navs, "loading": False, "downloads": 0, "focus": page.focus,
        "parents": {}, "lines": []})
    monkeypatch.setattr(browserd, "aim", lambda tab, target, parents: (50, 20, None))
    monkeypatch.setattr(browserd, "outline", lambda tab, chrome=True: {
        "view": {"y": 0, "h": 900, "page": 900}, "lines": ['- password field "Password" e14'],
        "scrolls": []})

    async def post(cfg, body):
        try:
            return browserd.do_call(json.loads(json.dumps(body)))
        except browserd.Problem as e:
            raise BrowserRefused(e.status, {"error": e.code, **e.facts}) from None
    monkeypatch.setattr(sync, "post", post)
    page.browser = Browser(vault_app.cfg)
    page.browser.app = vault_app.app.state.services
    return page


@pytest.mark.anyio
async def test_a_vault_entry_is_typed_with_no_draft_and_no_shot(box, monkeypatch, vault_app,
                                                                store):
    """A type with a placeholder into a password field, then the tab leaves the page: no
    "Text you typed into" line, no step shot, and the user's row says what was typed."""
    ctx = ctx_for(store, await conversations.create(store))
    await vault_app.skill.set(ctx, name="example-shop", url="https://x")
    pw = vault_app.bw.state()["items"][0]["login"]["password"]
    page = _login_page(box, monkeypatch, vault_app)
    b, shots = page.browser, page.shots

    out = await b.act(ctx, steps=[{"do": "type", "target": "e14",
                                   "text": "vault:example-shop/password\n"}], tab="a")
    assert ("Input.insertText", {"text": pw}) in box.sent
    assert out.display["steps"] == [{"step": "Typed vault:example-shop/password into e14",
                                     "effect": "nothing changed on the page"}]
    assert shots == [] and box.tab.drafts == []
    assert pw not in out.content and "type e14 vault:example-shop/password" in out.content
    assert out.content.endswith("A password field: vault__get or vault__set.")
    assert "vault" in await loads.touched(store, ctx.conversation_id)
    out = await b.open(ctx, url="https://x/home", tab="a")
    assert page.url == "https://x/home" and "Text you typed into" not in out.content
    page.url = "https://x/login"

    out = await b.act(ctx, steps=[{"do": "type", "target": "e14",
                                   "text": "plain-text-pass-1"}], tab="a")
    assert out.is_error and out.content == (
        "Nothing was typed into e14. A password field takes a vault entry: vault__set(name) "
        "makes one, vault__get(name) gives the text to type.")
    vault_app.bw.change(reachable=False, session="session-9")
    out = await b.act(ctx, steps=[{"do": "type", "target": "e14",
                                   "text": "vault:example-shop/password"}], tab="a")
    assert out.content == f"Nothing was typed: the vault is locked, tell {vault_app.cfg.user}."


@pytest.mark.anyio
async def test_a_missing_entry_names_its_tool(
        box, monkeypatch, vault_app, store):
    """A placeholder at the cursor is typed; one of an entry that is not there, or has no
    previous one, names the tool that fixes it, not the user."""
    ctx = ctx_for(store, await conversations.create(store))
    await vault_app.skill.set(ctx, name="example-shop", url="https://x")
    page = _login_page(box, monkeypatch, vault_app)
    b = page.browser
    page.focus = '- password field "Password" [active]'
    out = await b.act(ctx, steps=[{"do": "type", "text": "vault:example-shop/password"}], tab="a")
    assert out.display["steps"][0]["step"] == "Typed vault:example-shop/password at the cursor"
    out = await b.act(ctx, steps=[{"do": "type", "target": "e14",
                                   "text": "vault:nope/password"}], tab="a")
    assert out.is_error and out.content == (
        "Nothing was typed. There is no vault entry nope. vault__set(name) makes one.")
    out = await b.act(ctx, steps=[{"do": "type", "target": "e14",
                                   "text": "vault:example-shop/previous"}], tab="a")
    assert out.content == ("Nothing was typed. example-shop has no previous password: "
                           "vault__rotate(name) has not changed it yet.")


@pytest.mark.anyio
async def test_a_placeholder_takes_any_name_up_to_its_last_part(box, monkeypatch, vault_app,
                                                                store):
    """vault:<name>/password: the name is everything up to the last /password or
    /previous, capitals, spaces and a slash too; one that differs only by case is typed
    and its result and the user's row name the entry it matched."""
    vault_app.bw.change(items=by_hand("GitHub", "Github", "My Bank", "work/password"))
    ctx = ctx_for(store, await conversations.create(store))
    page = _login_page(box, monkeypatch, vault_app)
    page.url, b = "https://bank.example/login", page.browser

    async def typed(text: str) -> tuple[str, str]:
        n = len(box.sent)
        out = await b.act(ctx, steps=[{"do": "type", "target": "e14", "text": text}], tab="a")
        [pw] = [p["text"] for m, p in box.sent[n:] if m == "Input.insertText"]
        return pw, out.display["steps"][0]["step"]

    assert await typed("vault:work/password/password") == (
        "test-hand-made-0003", "Typed vault:work/password/password into e14")
    pw, row = await typed("vault:GITHUB/password")
    assert (pw, row) == ("test-hand-made-0000", "Typed vault:GitHub/password into e14")
    out = await b.act(ctx, steps=[{"do": "type", "target": "e14",
                                   "text": "vault:github/password\n"}], tab="a")
    assert "type e14 vault:GitHub/password" in out.content


@pytest.mark.anyio
async def test_a_vault_entry_is_typed_only_on_its_own_site(box, monkeypatch, vault_app, store):
    """The page and the frame that holds the cursor must be one of the entry's sites,
    exactly: another host, a subdomain, http, a user name in the address, a frame of
    another site, a page still loading, all refused before a key goes out, and the user
    is pinged. An entry with no site is typed nowhere. Typing asks no tap: the site check
    is its guard."""
    ctx = ctx_for(store, await conversations.create(store))
    await vault_app.skill.set(ctx, name="example-shop", url="https://example-shop.com")
    await vault_app.skill.set(ctx, name="nosite")
    page = _login_page(box, monkeypatch, vault_app)
    b = page.browser
    pings = []

    async def ping(text):
        pings.append(text)
    vault_app.app.state.services.approvals.ping = ping

    async def typed(url, frame=None, loading=False, entry="example-shop"):
        page.url, page.frame, box.tab.loading = url, frame, loading
        n = len(box.sent)
        out = await b.act(ctx, steps=[{"do": "type", "target": "e14",
                                       "text": f"vault:{entry}/password"}], tab="a")
        return out, [m for m, _ in box.sent[n:] if m == "Input.insertText"]

    out, keys = await typed("https://example-shop.com/login")
    assert keys == ["Input.insertText"] and not out.is_error
    out, keys = await typed("https://example-shop.com:443/other")
    assert keys == ["Input.insertText"]
    for url, frame in (("https://evil.example/login", None),
                       ("https://www.example-shop.com/login", None),
                       ("http://example-shop.com/login", None),
                       ("https://example-shop.com@evil.example/login", None),
                       ("https://example-shop.com:8443/login", None),
                       ("https://example-shop.com/login", "https://evil.example/frame"),
                       ("https://example-shop.com/login", "")):
        out, keys = await typed(url, frame)
        assert keys == [] and out.is_error, (url, frame)
        assert "not one of the entry's own sites" in out.content
    assert len(pings) == 7 and "example-shop" in pings[0] and "evil.example" in pings[0]
    out, keys = await typed("https://example-shop.com/login", loading=True)
    assert keys == [] and out.is_error
    out, keys = await typed("https://example-shop.com/login", entry="nosite")
    assert keys == [] and "nosite has no https site of its own" in out.content
    assert vault_app.taps == []


@pytest.mark.anyio
async def test_a_vault_entry_is_typed_only_into_a_password_field(box, monkeypatch, vault_app,
                                                                store):
    """By its id from its last outline, refused before anything runs; at the cursor or
    at a point, the element that holds the cursor is an <input type=password> right
    before the text goes out, else nothing is typed. The model hears why."""
    ctx = ctx_for(store, await conversations.create(store))
    await vault_app.skill.set(ctx, name="example-shop", url="https://x")
    page = _login_page(box, monkeypatch, vault_app)
    b = page.browser
    box.tab.nodes[15] = browserd.Node(15, "s1", 150, "textbox", (0, 0, 100, 40),
                                      SimpleNamespace(owner=None))
    words = "A vault entry is typed only into a password field."

    async def typed(steps):
        n = len(box.sent)
        out = await b.act(ctx, steps=steps, tab="a")
        return out, [m for m, _ in box.sent[n:] if m.startswith("Input.")]

    out, keys = await typed([{"do": "click", "target": "e14"},
                             {"do": "type", "target": "e15",
                              "text": "vault:example-shop/password"}])
    assert out.is_error and out.content == f"Nothing was typed into e15. {words}"
    assert keys == []
    page.password, page.focus = False, '- textbox "Search" [active]'
    out, keys = await typed([{"do": "type", "text": "vault:example-shop/password"}])
    assert out.is_error and out.content == f"Nothing was typed at the cursor. {words}"
    assert "Input.insertText" not in keys
    out, keys = await typed([{"do": "type", "target": "120,300",
                              "text": "vault:example-shop/password"}])
    assert out.is_error and out.content == (
        f"Nothing was typed into the field at 120,300. {words}")
    assert "Input.insertText" not in keys
    page.password = True
    out, keys = await typed([{"do": "type", "target": "120,300",
                              "text": "vault:example-shop/password"}])
    assert not out.is_error and "Input.insertText" in keys


def test_the_last_shot_is_taken_again_after_the_outline(box, monkeypatch):
    """Every step is shot after its effect; the last one once more after the outline
    waited for the page to settle, with the same aim, so what the user sees is the
    page it read (a login dialog drawn after the load event was blank before). A
    refused call keeps the shots it has."""
    shots = []
    monkeypatch.setattr(browserd, "step_shot", lambda tab, aimed: shots.append(aimed) or "s")
    monkeypatch.setattr(browserd, "fingerprint", lambda tab: {
        "navs": tab.navs, "loading": False, "downloads": 0, "focus": None, "parents": {},
        "lines": []})
    monkeypatch.setattr(browserd, "aim", lambda tab, target, parents: (50, 20, None))
    monkeypatch.setattr(browserd, "outline", lambda tab, chrome=True: {
        "view": {"y": 0, "h": 900, "page": 900}, "lines": ['- button "Go" e1'], "scrolls": []})
    box.answers["Target.getTargetInfo"] = lambda p: {"targetInfo": {"url": "https://x/",
                                                                    "title": "x"}}
    res = browserd.run_steps(box.tab, [{"do": "scroll", "text": "down"},
                                       {"do": "click", "target": "e1"}])
    assert shots == [None, (50, 20, None), (50, 20, None)]
    assert [e["shot"] for e in res["effects"]] == ["s", "s"] and res["done"] == 2
    shots.clear()
    monkeypatch.setattr(browserd, "aim", lambda tab, target, parents: (_ for _ in ()).throw(
        browserd.Problem(409, "disabled")))
    with pytest.raises(browserd.Problem) as e:
        browserd.run_steps(box.tab, [{"do": "scroll", "text": "down"},
                                     {"do": "click", "target": "e1"}])
    assert shots == [None] and e.value.facts["done"] == 1
