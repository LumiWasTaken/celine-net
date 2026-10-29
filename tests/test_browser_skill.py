"""The browser skill: its tools, their steps and words, the screen model."""
from __future__ import annotations

import ipaddress
import socket
from types import SimpleNamespace

import httpx
import pytest

from celine.browser import sync
from celine.browser.sync import BrowserRefused
from celine.skills import browser as browser_mod
from celine.skills.browser import Browser

pytestmark = pytest.mark.anyio


async def test_open_and_close_have_effects_scroll_and_look_read(monkeypatch):
    """A GET can log out or download, a close drops a tab: neither is reading."""
    for name in ("scroll", "look"):
        assert getattr(Browser, name).__celine_tool__["annotations"].read_only
    for name in ("open", "close"):
        hints = getattr(Browser, name).__celine_tool__["annotations"]
        assert not hints.read_only and not hints.destructive
    assert not Browser.act.__celine_tool__["annotations"].read_only
    sent = []

    async def post(cfg, body):
        sent.append(body)
        if body["steps"][0]["do"] == "close":
            return {"closed": body["tab"], "tabs": ["b"], "notes": []}
        return {"tab": body["tab"], "profile": "main", "url": "https://x/", "title": "x",
                "view": {"y": 600, "h": 800, "page": 3000}, "lines": ['- link "y" e1'],
                "notes": []}

    monkeypatch.setattr(sync, "post", post)
    b = Browser(SimpleNamespace(user="Joe", settings=None))
    ctx = SimpleNamespace(max_output_tokens=None)
    out = await b.scroll(ctx, tab="a")
    assert sent[-1] == {"tab": "a", "steps": [{"do": "scroll", "text": "down"}]}
    assert "screen 2 of 4: 0.8 above, 2.0 below" in out.content and '- link "y" e1' in out.content
    await b.scroll(ctx, to="e12", amount=-300, tab="a", show="main")
    assert sent[-1] == {"tab": "a", "steps": [{"do": "scroll", "target": "e12", "text": "-300"}],
                        "show": "main"}
    await b.scroll(ctx, direction="top", tab="a")
    assert sent[-1]["steps"] == [{"do": "scroll", "text": "top"}]
    out = await b.close(ctx, tab="a")
    assert sent[-1] == {"tab": "a", "steps": [{"do": "close"}]}
    assert out.content == "Closed tab a. Open tabs: b." and out.display == {"tab": "a", "closed": True}


async def test_the_words_match_the_box(monkeypatch):
    sent = []

    async def post(cfg, body):
        sent.append(body)
        if body.get("what") == "page":
            return {"tab": body["tab"], "url": "https://x/", "title": "x", "notes": [],
                    "markdown": "\n\n".join(f"Paragraph {i} " + "word " * 400 for i in range(30))}
        raise BrowserRefused(400, {"error": "no_select", "role": "combobox", "done": 0})

    monkeypatch.setattr(sync, "post", post)
    b = Browser(SimpleNamespace(user="Joe", settings=None))
    ctx = SimpleNamespace(max_output_tokens=None)
    out = await b.act(ctx, steps=[{"do": "select", "target": "e7", "text": "Large"}], tab="jobs")
    assert out.is_error and out.content == ("e7 is a combobox, not a select: click it and "
                                            "click the option.")
    out = await b.look(ctx, what="page", tab="jobs")
    assert 'later: browser__look(what="page", part=2, tab="jobs")' in out.content
    n = len(sent)
    out = await b.scroll(ctx, to="e8", direction="top", tab="jobs")
    assert out.is_error and len(sent) == n, "refused here, not sent"
    out = await b.act(ctx, steps=[{"do": "scroll", "target": "e8", "text": "bottom"}], tab="jobs")
    assert out.is_error and len(sent) == n


async def test_a_call_with_steps_waits_longer(monkeypatch):
    seen = []

    def client(cfg, read=sync.READ):
        seen.append(read)
        raise httpx.ConnectError("no box")
    monkeypatch.setattr(sync, "_client", client)
    for body in ({"tab": "a", "steps": [{"do": "wait", "text": "x"}] * 20},
                 {"tab": "a", "do": "look"}):
        with pytest.raises(httpx.ConnectError):
            await sync.post(None, body)
    assert seen == [sync.READ + 20 * sync.STEP_READ, sync.READ]


async def test_each_step_says_what_it_did_and_where_the_view_is(monkeypatch):
    """One effect line per step, the screen line instead of pixels, a refusal before a
    click with what covers the target."""
    async def post(cfg, body):
        if body["steps"][-1].get("target") == "e9":
            raise BrowserRefused(409, {"error": "covered", "done": 1, "effects": [
                {"effect": "none"}], "by": {"role": "dialog", "name": "Cookies", "id": "e3"}})
        return {"tab": "a", "profile": "main", "url": "https://x/q", "title": "Quiz",
                "view": {"y": 0, "h": 900, "page": 2700}, "lines": ['- button "Agree" e2'],
                "notes": [], "done": 3, "effects": [
                    {"effect": "changed", "added": 1, "gone": 1,
                     "lines": ['- heading "Question 2 of 46"']},
                    {"effect": "none"}, {"effect": "focus", "to": '- textbox "Name"'}]}

    monkeypatch.setattr(sync, "post", post)
    b = Browser(SimpleNamespace(user="Joe", settings=None))
    ctx = SimpleNamespace(max_output_tokens=None)
    out = await b.act(ctx, steps=[{"do": "click", "target": "e2"}, {"do": "click", "target": "e4"},
                                  {"do": "press", "text": "Tab"}], tab="a")
    assert out.content.splitlines()[1:6] == [
        "click e2: the page changed, 1 lines new, 1 gone:",
        '    heading "Question 2 of 46"',
        "click e4: nothing changed on the page",
        'press Tab: only the focus moved, to textbox "Name"',
        "screen 1 of 3: top, 2.0 below"]
    assert " px" not in out.content
    out = await b.act(ctx, steps=[{"do": "click", "target": "e2"}, {"do": "click", "target": "e9"}],
                      tab="a")
    assert out.is_error and out.content == (
        'Step 2 (click e9) was refused; steps 1 to 1 were done: e9 is covered by dialog '
        '"Cookies" e3: nothing was done. Answer or close what is on top first.\n'
        "click e2: nothing changed on the page")


def test_a_target_is_an_id_or_a_point_of_the_screen():
    """A target is an element id or a point x,y in px of the screen, from a vision
    model's box on 0 to 1000."""
    from celine import vision
    from celine.errors import ToolRefused
    from celine.skills.browser import in_view, target_of
    assert [target_of(t) for t in ("e1", "e5", "E2", "14")] == ["e1", "e5", "e2", "e14"]
    assert target_of("712, 340") == "712,340"
    for bad in ("1440,10", "F1/D3", "f1"):
        with pytest.raises(ToolRefused):
            target_of(bad)
    # every box form the screen models write, the first one in the answer
    for said in ('{"bbox_2d": [450, 360, 540, 400]}',
                 'Sure.\n```json\n[{"bbox_2d": [450,360,540,400], "label": "Sign in"}]\n```',
                 "[450, 360, 540, 400] and [1, 2, 3, 4]",
                 "<box>(450,360),(540,400)</box>",
                 "<box>450 360 540 400</box>",
                 "<|box_start|>(450,360),(540,400)<|box_end|>",
                 '{"bbox_2d": [540.4, 400, 450, 359.6]}'):
        assert vision.box(said) == (450, 360, 540, 400), said
    for said in ("I cannot see a Sign in button.", '{"bbox_2d": [450, 360]}',
                 '{"bbox_2d": [1200, 3, 1300, 40]}', '{"bbox_2d": [0.1, 0.2, 0.3, 0.4]}'):
        assert vision.box(said) is None, said
    assert vision.box("[0, 0, 1, 1]") == (0, 0, 1, 1), "whole numbers are on 0 to 1000"
    # scaled to the 1440x900 view, the edges kept inside it
    assert in_view("450,360,540,400") == ([713, 342], [648, 324, 778, 360])
    assert in_view("1000,1000,1000,1000") == ([1439, 899], [1439, 899, 1439, 899])


async def test_a_blind_model_hears_it_at_once_and_a_down_screen_server_does_not_hang(monkeypatch):
    """The agent's model takes no image: with no screen model either, one sentence says it
    cannot see the screen (not a second call that fails too); with one, it is told to
    ask a question; a question to a server that refuses fails at once, with the first
    model alone (no fallback loop waiting for a model), and says to tell the user."""
    import base64
    from celine import attachments, vision
    from celine.backends.base import BackendError

    async def blind(cfg, ctx):
        return False

    async def do(cfg, tab, action, **args):
        return {"tab": tab, "profile": "main", "url": "https://x/", "title": "x",
                "shot": base64.b64encode(b"png").decode(), "lines": [], "notes": [],
                "view": {"y": 0, "h": 900, "page": 900}}

    async def keep(self, ctx, image, filename, media_type):
        return {"id": "a1", "sha256": "s", "filename": filename, "media_type": media_type,
                "bytes": len(image), "width": 1440, "height": 900}

    monkeypatch.setattr(attachments, "turn_sees_images", blind)
    monkeypatch.setattr(sync, "do", do)
    monkeypatch.setattr(Browser, "_keep", keep)
    ctx = SimpleNamespace(max_output_tokens=None, conversation_id="", turn_id="")
    b = Browser(SimpleNamespace(user="Joe", settings=None))
    out = await b.look(ctx, what="screen", tab="g")
    assert out.is_error and out.content == (
        "You cannot see the screen: your model does not take images and no model is set "
        "for screen reading. Tell Joe. browser__look(what=\"view\") and what=\"page\" "
        "still work.")

    bound = []

    class Stream:
        answered = None

        async def stream(self, messages, tools):
            raise BackendError("gpu-box at 192.0.2.10:11434 refused the connection.")
            yield

    class Router:
        async def order(self, role):
            return SimpleNamespace(refs=["gpu-box/small-model"], sentence=lambda: "")

    class Llm:
        router = Router()
        turn_priority: dict = {}

        def bind(self, backend=None, **kw):
            bound.append(kw)
            return Stream()

    b.app = SimpleNamespace(llm=Llm())
    out = await b.look(ctx, what="screen", tab="g")
    assert out.is_error and out.content.startswith("Your model does not take images, so ask")
    out = await b.look(ctx, what="locate", question="the Play button", tab="g")
    assert out.is_error and out.content == (
        "The question was not asked: gpu-box at 192.0.2.10:11434 refused the connection. Tell "
        "Joe. browser__look(what=\"view\") and what=\"page\" still work.")
    assert bound[-1]["fallback"] is False and bound[-1]["role"] == "screen"
    with pytest.raises(vision.Unavailable):
        await vision.ask(None, b"png", "q", "text", conversation_id="", turn_id="")


def test_a_step_line_stays_one_line_and_does_not_repeat_the_address():
    """A newline it typed reads \\n; a step that went to the page on the head line
    says so (a Google address runs to a thousand characters, twice before)."""
    assert Browser._step_words({"do": "type", "target": "e2", "text": "python\n"}) == \
        "type e2 python\\n"
    long = "https://www.google.com/search?q=python&gs_lp=" + "E" * 900
    nav = {"effect": "navigated", "url": long}
    assert Browser._effect({"do": "open"}, nav, long) == "loaded the address above"
    assert Browser._effect({"do": "click", "target": "e2"}, nav, long) == "went to the address above"
    assert Browser._effect({"do": "click", "target": "e2"}, nav, "https://x/") == f"went to {long}"
    assert Browser._effect({"do": "click", "target": "e2"}, nav) == f"went to {long}"


def screen_model(answers: list[str], refs=("gpu-box/screen-model",)):
    """A fake LLM layer whose Screen reading model answers each question with the next
    of `answers`; the questions asked land in .asked."""
    from celine.backends.base import Done, TextDelta

    class Stream:
        answered = SimpleNamespace(model="screen-model")

        async def stream(self, messages, tools):
            Llm.asked.append(messages[0]["content"][0]["text"])
            yield TextDelta(answers.pop(0))
            yield Done("stop")

    class Router:
        async def order(self, role):
            return SimpleNamespace(refs=list(refs),
                                   sentence=lambda: "No model is set for Screen reading.")

    class Llm:
        router = Router()
        turn_priority: dict = {}
        asked: list = []

        def bind(self, backend=None, **kw):
            assert kw["role"] == "screen"
            return Stream()

    return Llm()


def fake_box(monkeypatch, log: list, refuse: dict | None = None):
    """The box: a shot is a look, steps answer an effect each; `refuse`: the facts a
    step with that target is refused with."""
    import base64

    def page(tab):
        return {"tab": tab, "profile": "main", "url": "https://x/", "title": "x",
                "view": {"y": 0, "h": 900, "page": 900}, "lines": ['- button "Sign in" e7'],
                "notes": []}

    async def do(cfg, tab, action, **args):
        log.append(("shot", args))
        return {**page(tab), "shot": base64.b64encode(b"png").decode()}

    async def post(cfg, body):
        log.append(("steps", body))
        for k, st in enumerate(body["steps"]):
            if refuse and st.get("target") == refuse["target"]:
                raise BrowserRefused(409, {**refuse["facts"], "done": k,
                                           "effects": [{"effect": "none"}] * k})
        return {**page(body["tab"]), "done": len(body["steps"]),
                "effects": [{"effect": "navigated", "url": "https://x/in"}
                            if st["do"] == "click" else {"effect": "none"}
                            for st in body["steps"]]}

    async def keep(self, ctx, image, filename, media_type):
        return {"id": "a1", "sha256": "s", "filename": filename, "media_type": media_type,
                "bytes": len(image), "width": 1440, "height": 900}

    monkeypatch.setattr(sync, "do", do)
    monkeypatch.setattr(sync, "post", post)
    monkeypatch.setattr(Browser, "_keep", keep)


async def test_locate_clicks_the_middle_of_the_box_the_screen_model_draws(monkeypatch):
    """A locate step: a plain shot of the view as it is when its turn comes, the screen
    model's box scaled to the view, a click at its centre (a point target), the steps
    before it sent first and those after it with the click; the result says where."""
    log: list = []
    fake_box(monkeypatch, log)
    b = Browser(SimpleNamespace(user="Joe", settings=None))
    b.app = SimpleNamespace(llm=screen_model(['```json\n{"bbox_2d": [450, 360, 540, 400]}\n```']))
    ctx = SimpleNamespace(max_output_tokens=None, conversation_id="c", turn_id="t")
    out = await b.act(ctx, steps=[{"do": "type", "target": "e5", "text": "sam"},
                                  {"do": "locate", "text": "the blue Sign in button"},
                                  {"do": "press", "text": "Enter"}], tab="a")
    assert not out.is_error
    assert log == [
        ("steps", {"tab": "a", "steps": [{"do": "type", "target": "e5", "text": "sam"}],
                   "show": "none"}),
        ("shot", {"what": "shot", "shot": "plain"}),
        ("steps", {"tab": "a", "steps": [{"do": "click", "target": "713,342"},
                                         {"do": "press", "text": "Enter"}]})]
    assert "Question: the blue Sign in button" in b.app.llm.asked[0]
    assert "bbox_2d" in b.app.llm.asked[0]
    assert out.content.splitlines()[1:4] == [
        "type e5 sam: nothing changed on the page",
        "locate the blue Sign in button: found at 713,342 (box 648,324 to 778,360), "
        "clicked there: went to https://x/in",
        "press Enter: nothing changed on the page"]

    # hover, and none: only where it is, nothing sent to the box
    log.clear()
    b.app = SimpleNamespace(llm=screen_model(["<box>(0,0),(10,10)</box>",
                                              "[990, 990, 1000, 1000]"]))
    out = await b.act(ctx, steps=[{"do": "locate", "text": "the menu", "then": "hover"},
                                  {"do": "locate", "text": "the corner", "then": "none"}],
                      tab="a")
    assert [e[1]["steps"] for e in log if e[0] == "steps"] == [
        [{"do": "hover", "target": "7,4"}]]
    assert out.content.splitlines()[1:3] == [
        "locate the menu: found at 7,4 (box 0,0 to 14,9), hovered there: nothing changed "
        "on the page",
        "locate the corner: found at 1432,895 (box 1426,891 to 1439,899)"]
    assert '- button "Sign in" e7' in out.content, "the page comes from the last shot"
    b.app = SimpleNamespace(llm=screen_model(["[990, 990, 1000, 1000]"]))
    out = await b.act(ctx, steps=[{"do": "locate", "text": "the corner", "then": "none"}],
                      tab="a", show="none")
    assert "Sign in" not in out.content, "show none: no outline from the shot either"


async def test_locate_fails_with_the_models_words_or_without_a_screen_model(monkeypatch):
    log: list = []
    fake_box(monkeypatch, log, refuse={"target": "713,342", "facts": {
        "error": "covered", "by": {"role": "dialog", "name": "Cookies", "id": "e3"}}})
    b = Browser(SimpleNamespace(user="Joe", settings=None))
    ctx = SimpleNamespace(max_output_tokens=None, conversation_id="c", turn_id="t")
    steps = [{"do": "click", "target": "e2"}, {"do": "locate", "text": "the Sign in button"}]

    b.app = SimpleNamespace(llm=screen_model(["I see no such button."]))
    out = await b.act(ctx, steps=steps, tab="a")
    assert out.is_error and out.content == (
        'Step 2 (locate the Sign in button) was refused; steps 1 to 1 were done: The screen '
        'reading model found no "the Sign in button" on the screen; nothing was done. It '
        'said: "I see no such button."\nclick e2: went to https://x/in')

    b.app = SimpleNamespace(llm=screen_model([], refs=()))
    out = await b.act(ctx, steps=steps[1:], tab="a")
    assert out.is_error and out.content == (
        "The question was not asked: No model is set for Screen reading. Tell Joe. "
        "browser__look(what=\"view\") and what=\"page\" still work.")

    b.app = SimpleNamespace(llm=screen_model(['{"bbox_2d": [450, 360, 540, 400]}']))
    out = await b.act(ctx, steps=steps, tab="a")
    assert out.is_error and out.content == (
        'Step 2 (locate the Sign in button) was refused; steps 1 to 1 were done: "the Sign '
        'in button" is covered by dialog "Cookies" e3: nothing was done. Answer or close '
        'what is on top first.\nclick e2: went to https://x/in')

    out = await b.act(ctx, steps=[{"do": "locate", "text": "x", "then": "drag"}], tab="a")
    assert out.is_error and "then is click, hover or none" in out.content


async def test_look_locate_gives_the_box_and_its_centre_and_clicks_nothing(monkeypatch):
    log: list = []
    fake_box(monkeypatch, log)
    b = Browser(SimpleNamespace(user="Joe", settings=None))
    b.app = SimpleNamespace(llm=screen_model(['{"bbox_2d": [450, 360, 540, 400]}']))
    ctx = SimpleNamespace(max_output_tokens=None, conversation_id="c", turn_id="t")
    out = await b.look(ctx, what="locate", question="the Play button", part=2,
                       wait="Play", tab="g")
    assert [e[0] for e in log] == ["shot"] and log[0][1]["screen"] == 2
    assert log[0][1]["wait"] == "Play" and out.display["model"] == "screen-model"
    assert out.content.splitlines()[1] == (
        'found at 713,342, box 648,324 to 778,360 (a click there: {"do": "click", '
        '"target": "713,342"})')
    assert out.display["point"] == [713, 342] and out.display["shot"]["id"] == "a1"
    out = await b.look(ctx, what="locate", tab="g")
    assert out.is_error and out.content.startswith("locate needs a question")


def _opener(monkeypatch, names: dict[str, list[str]]):
    """A browser whose box answers every open, and the names it looks up: (open, sent)."""
    sent = []

    async def post(cfg, body):
        sent.append(body["steps"][0]["url"])
        return {"tab": body["tab"], "profile": "main", "url": body["steps"][0]["url"],
                "title": "x", "view": {"y": 0, "h": 900, "page": 900}, "lines": [],
                "notes": []}

    async def status(cfg):
        return {"tabs": [{"name": "a"}]}

    async def addresses(host, port):
        return [ipaddress.ip_address(a) for a in names.get(host, [])]
    monkeypatch.setattr(sync, "post", post)
    monkeypatch.setattr(sync, "status", status)
    monkeypatch.setattr(browser_mod, "addresses", addresses)
    b = Browser(SimpleNamespace(user="Joe", settings=None))
    ctx = SimpleNamespace(max_output_tokens=None)

    async def open_(url):
        return await b.open(ctx, url=url, tab="a")
    return open_, sent


async def test_the_cloud_metadata_address_is_never_opened_the_lan_is(monkeypatch):
    """169.254.0.0/16 and fd00:ec2::254, written any way Brave reads an address, or a
    name that points there: refused before the box hears of it. The LAN opens."""
    open_, sent = _opener(monkeypatch, {"meta.example": ["169.254.169.254"],
                                        "nas.example": ["192.168.1.20"]})
    for url in ("http://169.254.169.254/latest/meta-data/", "169.254.170.2/v2/credentials",
                "http://[fd00:ec2::254]/latest/", "http://2852039166/", "http://169.254.43518/",
                "http://0xa9.0xfe.0xa9.0xfe/", "http://0251.0376.0251.0376/",
                "http://[::ffff:169.254.169.254]/", "http://meta.example/computeMetadata/v1/",
                "http://%31%36%39.254.169.254/", "view-source:http://169.254.169.254/",
                "http://169.254.169.254.:80/", "http://user@169.254.169.254/",
                "http://１６９。254.169.254/"):
        out = await open_(url)
        assert out.is_error and out.content == (
            "That is the cloud's instance metadata address, which no tab opens."), url
    assert sent == []
    for url in ("http://192.168.1.20:8080/", "http://nas.example/", "http://10.0.0.5/",
                "file:///home/celine/page.html", "chrome://settings", "brave://settings",
                "https://example.com/169.254.169.254"):
        out = await open_(url)
        assert not out.is_error, url
    assert len(sent) == 7


async def test_its_own_devtools_and_the_app_are_never_opened_its_shares_are(monkeypatch):
    """The DevTools port on the box's loopback, and the app where the box reaches it
    (shares.INSIDE, by that name or a name of one of the app's own addresses), refused
    before the box hears of it; a share of the app opens, as do the box's other ports
    and the app's name on another port."""
    mine = local_address()
    open_, sent = _opener(monkeypatch, {"localhost": ["127.0.0.1"],
                                        "app.example": [mine] if mine else []})
    for url in ("http://127.0.0.1:9300/json", "localhost:9300/json/version",
                "http://[::1]:9300/", "ws://127.0.0.1:9300/devtools/browser/x",
                "view-source:http://127.0.0.1:9300/json", "http://2130706433:9300/json",
                "http://127.1:9300/", "http://0.0.0.0:9300/json", "http://a.localhost:9300/"):
        out = await open_(url)
        assert out.is_error and out.content == (
            "That is your browser's own DevTools port, which no tab opens."), url
    app = ("That is the app itself, which no tab opens; a share of it does, at "
           "http://celine:8766/share/<token>/.")
    for url in ("http://celine:8766/api/agent", "http://CELINE.:8766/", "celine:8766",
                "https://celine:8766/", "http://celine:8766/share/../api/agent",
                "http://celine:8766/share/%2e%2e/api/agent", "http://celine:8766\\@evil.example/",
                *([f"http://{mine}:8766/", "http://app.example:8766/login"] if mine else [])):
        out = await open_(url)
        assert out.is_error and out.content == app, url
    assert sent == []
    for url in ("http://celine:8766/share/k3x9/", "http://celine:8766/share/k3x9/clip.mp4",
                "http://127.0.0.1:8000/", "http://localhost:3000/", "http://celine:9000/"):
        out = await open_(url)
        assert not out.is_error, url
    assert len(sent) == 5


def local_address() -> str | None:
    """An address of this machine that is not its loopback, None when it has none."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))       # no packet: it only picks the way out
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def test_an_address_is_the_apps_only_when_this_machine_has_it():
    assert not browser_mod.ours(ipaddress.ip_address("127.0.0.1"))
    assert not browser_mod.ours(ipaddress.ip_address("0.0.0.0"))
    assert not browser_mod.ours(ipaddress.ip_address("203.0.113.9"))
    if (mine := local_address()) is not None:
        assert browser_mod.ours(ipaddress.ip_address(mine))


async def test_the_vision_models_words_reach_her_as_it_said_them():
    from celine import vision
    from celine.backends.base import Done, TextDelta
    words = "I see  no such button.\n\nOnly a  logo." + " more" * 100

    class Bound:
        answered = SimpleNamespace(model="far-vision")

        async def stream(self, messages, tools):
            yield TextDelta(words)
            yield Done(reason="stop")

    llm = SimpleNamespace(
        router=SimpleNamespace(order=lambda role: _ordered()),
        bind=lambda *a, **k: Bound(), turn_priority={})

    async def _ordered():
        return SimpleNamespace(refs=["far"], sentence=lambda: "")
    got = await vision.ask(llm, b"png", "where is Sign in?", "box", conversation_id="c",
                           turn_id="t")
    assert got.value is None and got.said == words
