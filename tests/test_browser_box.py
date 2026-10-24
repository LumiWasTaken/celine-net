"""The browser box's recovery, case by case,
the live view with take-over through the app's relay (slice 3), throwaway tabs,
extensions and WebGL (slice 6), and the outline and steps on the test page (sota-2026).
Needs docker and builds the image locally (`-m browser`, about 5 minutes); the box is
test-browser, its home the volume test-browser-home, both removed afterwards. Run with
-s to see the times."""
from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from chat_fixtures import until_sync
from fastapi import FastAPI
from starlette.testclient import TestClient

from celine.browser import api, sync

pytestmark = pytest.mark.browser

ROOT = Path(__file__).parent.parent
IMAGE, BOX, HOME = "celine-browser-test", "test-browser", "test-browser-home"
KEY = "test-key-" + "b" * 16
PAGES = "http://127.0.0.1:8000"
# the test pages, served inside the box. /probe tells what a page sees (no CDP involved)
# and keeps the query in a cookie; /hog grows until something gives; /slow never ends;
# /ads tells whether a request to localhost got through; /games counts 2 s of frames;
# /page is the outline's page (a frame, shadow DOM, a form, a scroll area, prose with
# links; ?wall puts a cookie wall over it), /inner its frame; /missing answers 404.
SERVER = r'''
import http.server, json, time
PROBE = """<title>probe</title><script>
const r = w => fetch('/report', {method: 'POST', body: JSON.stringify({w, q: location.search,
  ua: navigator.userAgent, languages: navigator.languages,
  zone: Intl.DateTimeFormat().resolvedOptions().timeZone, inner: innerHeight,
  screen: [screen.width, screen.height], visible: document.visibilityState,
  cookie: document.cookie})});
r('start'); document.cookie = 'last=' + location.search.slice(1) + '; max-age=99999';
addEventListener('load', () => setTimeout(() => r('load'), 500));
</script>"""
PAGE = """<!doctype html><title>Test page</title>
<nav><a href=/page>Home</a> <a href=/inner>Inner</a></nav>
<main>
<h1>Welcome to the test page</h1>
<p>This paragraph has an <a href=/page#inline>inline link</a> and a <b>bold</b> word, then goes on.</p>
<ul><li>First item</li><li>Second item with a <a href=/page#li>link</a></li></ul>
<form onsubmit="event.preventDefault(); document.title = 'sent ' + new FormData(this).get('n')">
<label>Name <input name=n placeholder="your name"></label>
<label>Size <select name=s><option>Small<option selected>Medium<option>Large</select></label>
<label><input type=checkbox name=c checked> Keep me posted</label>
<input type=file name=f onchange="document.title = 'file ' + this.files[0].name">
<button type=submit>Send</button>
</form>
<my-widget></my-widget>
<div id=area style="height:120px;overflow-y:auto;border:1px solid"><p>Line 1</p><p>Line 2</p><p>Line 3</p><p>Line 4</p><p>Line 5</p><p>Line 6</p><p>Line 7</p></div>
<iframe src=/inner title="Inner frame" style="width:400px;height:150px"></iframe>
<div style="height:1500px">tall spacer</div>
<h2 id=bottom>Bottom heading</h2>
<p>The last words of the page.</p>
</main>
<footer><a href=/page#imprint>Imprint</a></footer>
<script>
customElements.define('my-widget', class extends HTMLElement { connectedCallback() {
  this.attachShadow({mode:'open'}).innerHTML = '<p>Shadow text</p><button onclick="document.title=\\'shadow\\'">Shadow button</button>'; } });
if (location.search === '?wall') {
  const w = document.createElement('div');
  w.style = 'position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center';
  w.innerHTML = '<div style="background:#fff;padding:40px"><h2>We use cookies</h2><p>Accept to continue.</p><button id=acc>Accept all</button> <button id=rej>Reject</button></div>';
  document.body.append(w);
  acc.onclick = rej.onclick = () => w.remove();
}
</script>"""
INNER = "<!doctype html><title>inner</title><p>Inside the frame</p><button onclick=\"this.textContent='Frame clicked'\">Frame button</button>"
PAGES = {"/plain": "<title>plain</title><a href=/plain>a link</a>",
         "/field": "<input placeholder=field style='width:1200px;height:700px'>",
         "/page": PAGE, "/inner": INNER,
         "/late": "<h2 id=h>Question 1</h2><button onclick=\"h.textContent='Question 2'\">Next"
                  "</button> <button>Nothing</button> <button disabled>Off</button><script>"
                  "setTimeout(() => document.body.insertAdjacentHTML('beforeend', '<div "
                  "role=dialog aria-label=Cookies style=\"position:fixed;inset:0\"></div>'), 4000)"
                  "</script>",
         "/json": '{"a": [1, 2],  "b": "x"}',
         "/hog": "<script>const a = [];"
                 "setInterval(() => a.push(new Uint8Array(100 << 20).fill(1)), 200)</script>",
         "/ads": "<script>fetch('http://localhost:8000/plain', {mode: 'no-cors'})"
                 ".then(() => 'loaded', () => 'blocked').then(got => fetch('/report', {method:"
                 "'POST', body: JSON.stringify({w: 'ads', q: location.search, got})}))</script>",
         "/games": "<canvas id=a></canvas><canvas id=b></canvas><script>let n = 0;"
                   "const c = a.getContext('2d'), gl = b.getContext('webgl'), t = performance.now();"
                   "(function frame() { c.fillRect(n % 300, 0, 9, 9); gl.clear(gl.COLOR_BUFFER_BIT);"
                   "gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(4)); n++;"
                   "if (performance.now() - t < 2000) return requestAnimationFrame(frame);"
                   "const i = gl.getExtension('WEBGL_debug_renderer_info');"
                   "fetch('/report', {method: 'POST', body: JSON.stringify({w: 'games', q: '',"
                   "fps: n / 2, renderer: gl.getParameter(i.UNMASKED_RENDERER_WEBGL)})}) })()</script>"}
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        self.send_response(404 if path == "/missing" else 200)
        self.send_header("Content-Type", "application/json" if path == "/json" else
                         "text/html; charset=utf-8"); self.end_headers()
        if path == "/slow":
            self.wfile.write(b"<p>half a page"); self.wfile.flush(); time.sleep(600)
        self.wfile.write(PAGES.get(path, PROBE).encode())
    def do_POST(self):
        with open("/tmp/reports.jsonl", "ab") as f:
            f.write(self.rfile.read(int(self.headers["Content-Length"])) + b"\n")
        self.send_response(204); self.end_headers()
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("127.0.0.1", 8000), H).serve_forever()
'''
# an extension of its own, run inside the box: one rule, no requests to localhost
EXTENSION = r'''
import json, pathlib
d = pathlib.Path.home() / ".browser-extensions" / "block"
d.mkdir(parents=True)
(d / "manifest.json").write_text(json.dumps({"name": "block", "version": "1",
    "manifest_version": 3, "permissions": ["declarativeNetRequest"], "declarative_net_request":
    {"rule_resources": [{"id": "r", "enabled": True, "path": "rules.json"}]}}))
(d / "rules.json").write_text(json.dumps([{"id": 1, "action": {"type": "block"},
    "condition": {"urlFilter": "||localhost^"}}]))
'''


def docker(*args: str, check: bool = True) -> str:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=900)
    if check and r.returncode:
        raise RuntimeError(f"docker {' '.join(args)}: {r.stderr}")
    return r.stdout


def hash_file() -> Path:
    """The key's sha256 in a file, as compose mounts it from the volume."""
    p = Path(tempfile.gettempdir()) / f"{BOX}.key.hash"
    p.write_text(hashlib.sha256(KEY.encode()).hexdigest() + "\n")
    return p


def run_box() -> None:
    """The box as compose runs it, with a new default hostname every time."""
    docker("run", "-d", "--name", BOX, "--init", "--memory", "3g", "--memory-swap", "3g",
           "--cpus", "2", "--pids-limit", "1024", "--tmpfs", "/tmp:size=1g,mode=1777",
           "--security-opt", "seccomp=unconfined", "-v", f"{HOME}:/home/celine",
           "-e", "TZ=Europe/Berlin", "-p", "127.0.0.1::8771", "-p", "127.0.0.1::5900",
           "-v", f"{hash_file()}:/etc/browser/key.hash:ro", IMAGE)
    serve()


def serve() -> None:
    docker("exec", "-d", BOX, "python3", "-c", SERVER)


def port(inner: int) -> int:
    return int(docker("port", BOX, str(inner)).split("\n")[0].rsplit(":", 1)[1])


def call(path: str, body: dict | None = None, wait: float = 30) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port(8771)}{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=wait) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def do(tab: str, *steps: dict, **first) -> tuple[int, dict]:
    """Steps on a tab; `first` (do=..., url=...) is one step before them."""
    return call("/do", {"tab": tab, "steps": ([first] if first else []) + list(steps)})


def open_(tab: str, url: str, **extra) -> tuple[int, dict]:
    return call("/do", {"tab": tab, "steps": [{"do": "open", "url": url}], **extra})


def look(tab: str, what: str = "view", **extra) -> tuple[int, dict]:
    return call("/do", {"tab": tab, "do": "look", "what": what, **extra})


def line(res: dict, part: str) -> str:
    return next((ln for ln in res.get("lines") or [] if part in ln), "")


def id_of(res: dict, part: str) -> str:
    return "e" + line(res, part).split(" e")[1].split(" ")[0].rstrip(":")


def back(since: float) -> tuple[float, dict]:
    """Seconds from `since` until a call on tab a answers, and its answer."""
    while time.monotonic() - since < 60:
        try:
            code, res = look("a")
            if code == 200:
                return time.monotonic() - since, res
        except (OSError, RuntimeError, ValueError):
            pass                         # browserd or the whole box is restarting
        time.sleep(0.25)
    raise AssertionError(docker("logs", BOX, check=False))


def state() -> str:
    try:
        return call("/status")[1]["state"]
    except (OSError, ValueError):
        return "gone"


def restarted(case: str, since: float, limit: float) -> None:
    """Back within the limit, with the note, its tabs at their last URLs."""
    took, res = back(since)
    print(f"case {case}: back in {took:.1f} s")
    assert took < limit
    assert any(n.startswith("restarted:") for n in res["notes"]), res
    tabs = {t["name"]: t["url"] for t in call("/status")[1]["tabs"]}
    assert tabs == {"a": f"{PAGES}/plain", "b": f"{PAGES}/plain?b"}


def brave(signal: str) -> None:
    docker("exec", BOX, "pkill", f"-{signal}", "-o", "-x", "brave")


def memory() -> int:
    return int(docker("exec", BOX, "cat", "/sys/fs/cgroup/memory.current"))


def report(q: str, w: str) -> dict:
    """A test page's report, once it came."""
    for _ in range(40):
        seen = [json.loads(line) for line in docker("exec", BOX, "cat", "/tmp/reports.jsonl",
                                                    check=False).splitlines()]
        if (hit := next((r for r in seen if r["q"] == q and r["w"] == w), None)):
            return hit
        time.sleep(0.25)
    raise AssertionError(f"no {w} report for {q!r}")


def probe(case: str) -> dict:
    """Test 9: in tab a, not the newest, so it is in front only if browserd put it there."""
    assert open_("a", f"{PAGES}/probe?{case}")[0] == 200
    load, start = report(f"?{case}", "load"), report(f"?{case}", "start")
    assert "HeadlessChrome" not in load["ua"]
    assert load["zone"] == "Europe/Berlin" and load["languages"] == ["en-US"]   # Brave: the first
    assert load["inner"] == start["inner"] == 900    # the screen: Brave tells a common one
    assert load["visible"] == "visible"
    open_("a", f"{PAGES}/plain")
    return start


@pytest.fixture(scope="module")
def box():
    if shutil.which("docker") is None:
        pytest.skip("no docker")
    docker("build", "-q", "-t", IMAGE, "--build-arg", "HER_UID=1000", "-f",
           "browser/Dockerfile", str(ROOT))
    docker("rm", "-f", BOX, check=False)
    docker("volume", "rm", HOME, check=False)
    run_box()
    while state() != "up":
        time.sleep(0.25)
    assert open_("a", f"{PAGES}/plain")[0] == 200
    assert open_("b", f"{PAGES}/plain?b")[0] == 200
    yield
    docker("rm", "-f", BOX, check=False)
    docker("volume", "rm", HOME, check=False)


def test_9_a_fresh_box_looks_like_a_browser(box):
    probe("fresh")


def test_the_outline_and_the_steps(box):
    """sota-2026: a cookie wall hides the page; ids stay; steps run in order and the
    refused one is named; select, find, upload, a frame's button, a shadow button, the
    page as Markdown, the HTML with shadow roots, an HTTP status and a load error."""
    since = time.monotonic()
    code, res = open_("a", f"{PAGES}/page?wall")
    print(f"outline: the page in {time.monotonic() - since:.2f} s, {len(res['lines'])} lines")
    assert code == 200 and res["status"] == 200 and res["error"] is None
    assert res["view"] == {"y": 0, "h": 900, "page": res["view"]["page"]}
    assert line(res, "Accept all") == '- button "Accept all" e1' and "Home" not in "\n".join(res["lines"])
    code, res = do("a", {"do": "click", "target": "e1"})
    text = "\n".join(res["lines"])
    assert '- link "Home" e3 [new]' in text and "inline link[e" in text
    assert 'frame "Inner frame":' in text and 'button "Frame button"' in text
    assert "(options: Small, Medium, Large)" in text and "scrollarea e" in text
    name, size, frame, shadow = (id_of(res, p) for p in ('textbox "Name"', 'select "Size"',
                                                          "Frame button", "Shadow button"))
    # steps in order, one result; a select by text; a frame's button by its id
    code, res = do("a", {"do": "type", "target": name, "text": "Celine"},
                   {"do": "select", "target": size, "text": "lar"},
                   {"do": "click", "target": frame})
    assert code == 200 and res["done"] == 3
    assert line(res, 'textbox "Name"') == f'      - textbox "Name" {name}: Celine [active]' or \
        f'textbox "Name" {name}: Celine' in line(res, 'textbox "Name"')
    assert f'select "Size" {size}: Large' in line(res, 'select "Size"')
    assert "Frame clicked" in line(res, "Frame clicked")
    code, res = do("a", {"do": "click", "target": shadow})
    assert res["title"] == "shadow"
    # a refused step says where it stopped; the steps before it were done
    code, res = do("a", {"do": "type", "target": name, "text": "Joe"},
                   {"do": "click", "target": "e999"})
    assert code == 400 and res == {"error": "bad", "what": "target", "ids": res["ids"], "done": 1,
                                   "effects": res["effects"]}
    assert res["effects"][0]["effect"] == "changed"
    assert "Joe" in line(look("a")[1], 'textbox "Name"')
    # find scrolls, the id of a thing scrolled away still works, top comes back
    code, res = do("a", {"do": "find", "text": "last words"})
    assert res["effects"][0]["effect"] == "found" and res["view"]["y"] > 1000
    assert "The last words of the page." in "\n".join(res["lines"])
    code, res = do("a", {"do": "find", "text": "no such words"})
    assert res["effects"][0]["effect"] == "not_found"
    code, res = do("a", {"do": "type", "target": name, "text": "Back\n"})
    assert res["title"] == "sent Back" and res["view"]["y"] < 400
    # upload into a file field, by its id
    docker("exec", BOX, "sh", "-c", "echo hi > /home/celine/cv.txt")
    code, res = do("a", {"do": "upload", "target": id_of(res, "Choose File"), "text": "cv.txt"})
    assert res["title"] == "file cv.txt", res["title"]
    # the page as Markdown, the HTML with shadow roots
    code, res = look("a", "page")
    md = res["markdown"]
    assert md.startswith("# Welcome to the test page") and "Shadow text" in md and "Inside the frame" in md
    assert "## Bottom heading" in md and "Imprint" not in md
    code, res = look("a", "html")
    assert "<my-widget>" in res["html"] and "Shadow button" in res["html"]
    # a status and a load error on the first line's facts
    code, res = open_("a", f"{PAGES}/missing")
    assert res["status"] == 404 and res["error"] is None
    code, res = open_("a", "http://127.0.0.1:8099/")
    assert res["status"] is None and res["error"] == "net::ERR_CONNECTION_REFUSED"
    code, res = do("a", {"do": "back"}, {"do": "back"}, {"do": "forward"})
    assert code == 200 and res["done"] == 3 and res["url"] == f"{PAGES}/missing"
    # marks: the ids drawn on a shot of the view, the lines along
    code, res = look("a", "shot", shot="marks")
    assert code == 200 and len(res["shot"]) > 1000 and "lines" in res
    open_("a", f"{PAGES}/plain")


def test_each_step_says_what_it_did_and_a_covered_target_is_not_clicked(box):
    """The quiz: a text swapped in place is a change, a button that does nothing says
    so, a disabled one and one under a wall that came late are not clicked; the source
    as served; a screen of the page."""
    code, res = open_("a", f"{PAGES}/late")
    nxt, nothing, off = (id_of(res, p) for p in ('"Next"', '"Nothing"', '"Off"'))
    code, res = do("a", {"do": "click", "target": nxt}, {"do": "click", "target": nothing})
    first, second = res["effects"]
    assert first["effect"] == "changed" and first["lines"] == ['- heading "Question 2" [level=2]']
    assert second["effect"] in ("none", "focus") and len(second["shot"]) > 1000
    code, res = do("a", {"do": "click", "target": off})
    assert (code, res["error"]) == (409, "disabled")
    time.sleep(4)
    code, res = do("a", {"do": "click", "target": nxt})
    assert (code, res["error"], res["by"]) == (409, "covered", {"role": "dialog", "name": "Cookies"})
    open_("a", f"{PAGES}/json")
    code, res = look("a", "source")
    assert res["source"] == '{"a": [1, 2],  "b": "x"}'
    open_("a", f"{PAGES}/page")
    code, res = look("a", "shot", screen=2)
    assert code == 200 and res["view"]["y"] == 900
    code, res = look("a", "shot", screen=9)
    assert (code, res["error"], res["what"]) == (400, "bad", "screen")
    open_("a", f"{PAGES}/plain")


def test_1_a_page_out_of_memory_costs_that_page(box):
    oom = int(docker("exec", BOX, "grep", "oom_kill ", "/sys/fs/cgroup/memory.events").split()[1])
    open_("b", f"{PAGES}/hog")
    for _ in range(80):
        if int(docker("exec", BOX, "grep", "oom_kill ", "/sys/fs/cgroup/memory.events")
               .split()[1]) > oom:
            break
        time.sleep(0.25)
    code, res = look("b")
    assert code == 200 and res["notes"] == ["crashed:b:killed"], res
    open_("b", f"{PAGES}/plain?b")           # the hog again, stopped
    until_sync(lambda: memory() < 1 << 30, 10)
    print(f"case 1: box memory {memory() >> 20} MB after the crash")
    code, res = look("a")
    assert code == 200 and res["url"] == f"{PAGES}/plain" and res["notes"] == []
    probe("1")


def test_2_brave_killed_mid_load_comes_back_with_an_older_cookie(box):
    probe("2")                                       # sets last=2
    time.sleep(60)                                   # cookies reach the disk every ~30 s
    answered = []
    slow = threading.Thread(target=lambda: answered.append(open_("b", f"{PAGES}/slow")))
    slow.start()
    time.sleep(2)
    since = time.monotonic()
    brave("KILL")
    slow.join()
    assert answered[0] == (503, {"error": "gone"})                  # the call in flight
    took, res = back(since)
    print(f"case 2: back in {took:.1f} s")
    assert took < 15 and any(n.startswith("restarted:") for n in res["notes"])
    tabs = {t["name"]: t["url"] for t in call("/status")[1]["tabs"]}
    assert tabs == {"a": f"{PAGES}/plain", "b": f"{PAGES}/slow"}
    assert "last=2" in probe("2b")["cookie"]
    open_("b", f"{PAGES}/plain?b")


def test_3_brave_stopped(box):
    since = time.monotonic()
    brave("STOP")
    restarted("3", since, 20)
    probe("3")


def test_4_browserd_killed(box):
    since = time.monotonic()
    docker("exec", BOX, "pkill", "-KILL", "-f", "/usr/local/bin/browserd")
    restarted("4", since, 15)
    probe("4")


def test_5_browserd_stopped(box):
    since = time.monotonic()
    docker("exec", BOX, "pkill", "-STOP", "-f", "/usr/local/bin/browserd")
    restarted("5", since, 20)
    probe("5")


def test_6_xvnc_killed(box):
    since = time.monotonic()
    docker("exec", BOX, "pkill", "-x", "Xvnc")
    restarted("6", since, 15)
    probe("6")


def test_7_the_whole_box_killed_and_recreated(box):
    since = time.monotonic()
    docker("kill", BOX)
    docker("start", BOX)
    serve()
    restarted("7 (docker kill, start)", since, 20)
    since = time.monotonic()
    docker("rm", "-f", BOX)
    run_box()                                        # another hostname: the lock is stale
    restarted("7 (a new box)", since, 20)
    probe("7")


def test_8_a_launch_that_never_answers_until_restart(box):
    swap = ("mv /usr/bin/brave-browser /usr/bin/brave-browser.real && "
            "printf '#!/bin/sh\\nexec sleep 999\\n' > /usr/bin/brave-browser && chmod 755 /usr/bin/brave-browser")
    docker("exec", "-u", "root", BOX, "sh", "-c", swap)
    assert do("a", do="restart") == (200, {"restarting": True})
    since = time.monotonic()
    while state() != "down":
        assert time.monotonic() - since < 30
        time.sleep(0.25)
    took = time.monotonic() - since
    print(f"case 8: down after {took:.1f} s")
    assert 19 < took < 25 and call("/status")[1]["reason"] == "no_answer"
    assert look("a")[0] == 503
    docker("exec", "-u", "root", BOX, "mv", "/usr/bin/brave-browser.real", "/usr/bin/brave-browser")
    assert do("a", do="restart") == (200, {"restarting": True})
    took, res = back(time.monotonic())
    print(f"case 8: up again {took:.1f} s after the restart")
    tabs = {t["name"]: t["url"] for t in call("/status")[1]["tabs"]}
    assert tabs == {"a": f"{PAGES}/plain", "b": f"{PAGES}/plain?b"}
    probe("8")


def screen(client: TestClient, w: int = 1441, h: int = 982) -> int:
    """Through the app's relay: the RFB handshake and one whole frame (raw), then a click
    into the field and the key x. The frame's bytes."""
    with client.websocket_connect("/ws/browser", headers={"host": "127.0.0.1"}) as ws:
        buf = bytearray()

        def read(n: int) -> bytes:
            while len(buf) < n:
                buf.extend(ws.receive_bytes())
            out = bytes(buf[:n])
            del buf[:n]
            return out

        assert read(12) == b"RFB 003.008\n"
        ws.send_bytes(b"RFB 003.008\n")
        assert read(read(1)[0]) == b"\x01"                    # security: None
        ws.send_bytes(b"\x01")
        assert read(4) == bytes(4)
        ws.send_bytes(b"\x01")                                # shared
        assert struct.unpack(">HH", read(4)) == (w, h)
        read(16)
        read(struct.unpack(">I", read(4))[0])
        ws.send_bytes(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
        assert read(2)[0] == 0                                # a framebuffer update
        area = size = 0
        for _ in range(struct.unpack(">H", read(2))[0]):
            _, _, rw, rh, _ = struct.unpack(">HHHHi", read(12))
            read(rw * rh * 4)
            area, size = area + rw * rh, size + 12 + rw * rh * 4
        assert area == w * h
        for buttons in (0, 1, 0):
            ws.send_bytes(struct.pack(">BBHH", 5, buttons, 640, 400))
        for down in (1, 0):
            ws.send_bytes(struct.pack(">BBxxI", 4, down, ord("x")))
        time.sleep(0.5)
    return size


def test_live_view_and_take_over(box, monkeypatch, stack_key):
    """Slice 3: the app's relay carries the screen to a client and its input back only
    while the user has the browser; its calls get 409 meanwhile, the note after give."""
    from celine.boot import load
    stack_key("browser", KEY)
    cfg = load()
    cfg.settings.services = {"browser": {"url": f"http://127.0.0.1:{port(8771)}"}}
    monkeypatch.setattr(sync, "VNC_PORT", port(5900))
    app = FastAPI()
    app.state.allowed_origins = set()

    async def no_rows(fn):
        return {}                         # no tab rows: every tab reads as the user's own
    app.state.services = SimpleNamespace(cfg=cfg, store=SimpleNamespace(read=no_rows))
    app.include_router(api.router)
    app.include_router(api.ws_router)
    assert open_("a", f"{PAGES}/field")[0] == 200
    with TestClient(app) as client:
        since = time.monotonic()
        size = screen(client)
        print(f"live view: a whole frame, {size / 1e6:.1f} MB, in {time.monotonic() - since:.2f} s")
        assert line(look("a")[1], "textbox") == '- textbox "field" e1'       # watching: no x
        taken = client.post("/api/browser/control", json={"action": "take"}).json()["taken"]
        assert client.get("/api/browser").json()["taken"] == taken
        assert look("a") == (409, {"error": "taken", "since": taken})
        screen(client)
        assert client.post("/api/browser/control", json={"action": "give"}).json() == \
            {"taken": None}
    code, res = look("a")
    assert code == 200 and line(res, "textbox").startswith('- textbox "field" e1: x')
    assert len(res["notes"]) == 1 and res["notes"][0].startswith(f"taken:{taken}:")
    open_("a", f"{PAGES}/plain")


def test_throwaway_tab_and_extensions(box):
    """Slice 6: a throwaway tab has none of the main cookies and is gone after a restart,
    with a note; a folder in ~/.browser-extensions loads at the restart."""
    probe("main")                                    # sets last=main
    assert open_("t", f"{PAGES}/probe?t", profile="throwaway")[0] == 200
    assert report("?t", "start")["cookie"] == ""
    open_("a", f"{PAGES}/ads?before")
    assert report("?before", "ads")["got"] == "loaded"
    docker("exec", BOX, "python3", "-c", EXTENSION)
    assert do("a", do="restart") == (200, {"restarting": True})
    took, res = back(time.monotonic())
    print(f"slice 6: up {took:.1f} s after the restart, with the extension")
    assert res["notes"] == ["throwaway:t"]
    assert {t["name"] for t in call("/status")[1]["tabs"]} == {"a", "b"}
    open_("a", f"{PAGES}/ads?after")
    assert report("?after", "ads")["got"] == "blocked"
    open_("a", f"{PAGES}/plain")


def test_a_canvas_and_webgl_run(box):
    open_("a", f"{PAGES}/games")
    games = report("", "games")
    print(f"slice 6: {games['fps']} frames a second, {games['renderer']}")
    assert games["fps"] > 20             # Brave tells pages "Brave" for the renderer
    open_("a", f"{PAGES}/plain")
