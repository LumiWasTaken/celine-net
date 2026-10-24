"""The outline and the page text from saved trees (tests/data/browser: the test page of
test_browser_box.py, dumped from the box with its hit tests), with no browser."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA = ROOT / "tests" / "data" / "browser"

spec = importlib.util.spec_from_file_location("snapshot", ROOT / "browser" / "snapshot.py")
snapshot = importlib.util.module_from_spec(spec)
sys.modules["snapshot"] = snapshot
spec.loader.exec_module(snapshot)


def load(name: str, edit=None):
    d = json.loads((DATA / f"{name}.json").read_text())
    if edit is not None:
        edit(d)
    top = snapshot.assemble(d["ax"], d["snap"], d["frames"], "top")
    vv, cs = d["metrics"]["cssVisualViewport"], d["metrics"]["cssContentSize"]
    view = snapshot.View(vv["pageX"], vv["pageY"], vv["clientWidth"], vv["clientHeight"], cs["height"])
    parents = {}
    for doc in d["snap"]["documents"]:
        nodes = doc["nodes"]
        backend = nodes["backendNodeId"]
        for i, p in enumerate(nodes["parentIndex"]):
            if p >= 0:
                parents[backend[i]] = backend[p]
        for i, child in zip(nodes["contentDocumentIndex"]["index"], nodes["contentDocumentIndex"]["value"]):
            parents[d["snap"]["documents"][child]["nodes"]["backendNodeId"][0]] = backend[i]
    hits = d["hits"]

    def reaches(frame, backend, x, y):
        hit = hits.get(f"{int(x)},{int(y)}")
        while hit is not None and hit != backend:
            hit = parents.get(hit)
        return hit == backend

    return top, view, reaches


def outline(name: str, ids=None, edit=None):
    top, view, reaches = load(name, edit)
    return snapshot.render(top, view, ids or snapshot.Ids(), reaches)


def test_a_password_input_is_a_password_field():
    """<input type=password> reads "password field" and its node says so: browserd takes
    nothing but a vault entry into it, and its browser skill points it at the vault."""
    def to_password(d):
        strings, nodes = d["snap"]["strings"], d["snap"]["documents"][0]["nodes"]
        strings += ["type", "PASSWORD"]
        nodes["attributes"][43] += [len(strings) - 2, len(strings) - 1]   # the Name input
    got = outline("page", edit=to_password)
    [line] = [ln for ln in got.lines if '"Name"' in ln]
    assert line.lstrip().startswith('- password field "Name" e')
    assert [n.role for n in got.nodes.values() if n.password] == ["textbox"]
    assert 'textbox "Name" e' in "\n".join(outline("page").lines)


def test_a_frame_leaves_the_page_password_fields():
    """After the walk enters an <iframe> and comes back, the page's own password fields
    are still the page's: a password input after a frame stays a password field."""
    def to_password(d):
        strings, nodes = d["snap"]["strings"], d["snap"]["documents"][0]["nodes"]
        strings += ["type", "PASSWORD"]
        nodes["attributes"][43] += [len(strings) - 2, len(strings) - 1]
    top, view, reaches = load("page", to_password)
    w = snapshot._Walker(view, snapshot.Ids(), reaches, False, snapshot.Rendered([], {}, []))
    w.frame(top)
    page = set(w.passwords)
    [iframe] = [n for n in top.ax if n["role"]["value"] == "Iframe"]
    assert w.iframe(iframe)
    assert page and w.passwords == page


def test_page_outline():
    got = outline("page")
    text = "\n".join(got.lines)
    assert "- navigation:\n  - link \"Home\" e1\n  - link \"Inner\" e2" in text
    assert 'heading "Welcome to the test page" [level=1]' in text
    assert "text: This paragraph has an inline link[e" in text          # a link in prose
    assert "- list:\n    - text: First item\n    - text: Second item with a link[e" in text
    assert 'textbox "Name" e' in text and "your name" not in text
    assert 'select "Size" e' in text and ": Medium (options: Small, Medium, Large)" in text
    assert 'checkbox "Keep me posted" e' in text and "[checked]" in text
    assert 'button "Shadow button" e' in text                           # shadow DOM
    assert 'frame "Inner frame":\n    - text: Inside the frame\n    - button "Frame button" e' in text
    assert "scrollarea e" in text and "px below" in text and got.scrolls
    assert "Bottom heading" not in text                                 # below the view
    sel = next(n for n in got.nodes.values() if n.role == "select")
    assert sel.options == ["Small", "Medium", "Large"]
    frame_button = next(n for n in got.nodes.values() if n.role == "button" and n.frame.doc == 1)
    assert frame_button.frame.offset[1] > 0


def test_a_wall_hides_the_page_ids_stay_and_new_is_marked():
    ids = snapshot.Ids()
    first = outline("wall", ids)
    text = "\n".join(first.lines)
    assert 'button "Accept all" e1' in text and 'button "Reject" e2' in text
    assert "Home" not in text and "Welcome" not in text, "the wall hides the page"
    assert len(first.nodes) == 2
    assert "[new]" not in text                                 # a first look marks nothing
    second = outline("page", ids)
    text = "\n".join(second.lines)
    assert 'link "Home" e3 [new]' in text                      # e1 and e2 were the wall's
    third = outline("page", ids)
    assert "[new]" not in "\n".join(third.lines)
    assert [ln for ln in third.lines if "Home" in ln] == [ln for ln in second.lines if "Home" in ln and "new" not in ln] or \
        'link "Home" e3' in "\n".join(third.lines)


def test_page_markdown():
    top, view, _ = load("page")
    md = snapshot.page(top, view)
    assert md.startswith("# Welcome to the test page")
    assert "This paragraph has an inline link and a bold word, then goes on." in md
    assert "- First item\n- Second item with a link" in md
    assert "Shadow text" in md and "Inside the frame" in md
    assert "## Bottom heading" in md and "The last words of the page." in md
    assert "Home" not in md and "Imprint" not in md                 # navigation and footer
    assert "Send" not in md and "Shadow button" not in md           # buttons are not reading


def test_scope_find_and_main():
    top, view, reaches = load("page")
    ids = snapshot.Ids()
    full = snapshot.render(top, view, ids, reaches)
    # main only: no navigation, no footer
    main = snapshot.render(top, view, ids, reaches, chrome=False)
    assert "navigation" not in "\n".join(main.lines) and "Home" not in "\n".join(main.lines)
    assert 'heading "Welcome to the test page"' in "\n".join(main.lines)
    assert len(main.lines) < len(full.lines)
    # one subtree, the whole of it: the frame's content under its <iframe>
    frame = next(n for n in full.nodes.values() if n.role == "button" and n.frame.doc == 1)
    sub = snapshot.render(top, view, ids, reaches, scope=(frame.session, frame.backend))
    assert sub.lines == [f'- button "Frame button" e{frame.id}']
    # find: the matching lines of the whole page with a line around each, gaps as ...
    whole = snapshot.render(top, view, ids, lambda *a: True, whole=True).lines
    found, n = snapshot.find(whole, "last words")
    assert any("The last words of the page." in ln for ln in found) and len(found) <= 4
    assert n == 1, "the lines around a hit are not hits"
    found, n = snapshot.find(whole, "/item/i")
    assert [ln for ln in found if "item" in ln.lower()] and "..." in found
    assert n == len([ln for ln in found if "item" in ln.lower()])
    assert snapshot.find(whole, "nothing of the sort") == ([], 0)


def test_fields_inside_a_zero_size_clickable_shadow_host_are_in_view():
    """tests/data/browser/shadow.html, dumped from headless Chromium with browserd's calls:
    a login dialog, position fixed, inside a shadow host with a click listener whose own
    box is 0x0 (reddit's faceplate-form), its fields in nested open and closed shadow
    roots. The host's empty box does not hide what is in it, and each id is the inner
    <input>, the node a click aims at."""
    got = outline("shadow")
    text = "\n".join(got.lines)
    assert '- dialog "Log In":' in text
    assert 'textbox "Username" e' in text and 'password field "Password" e' in text
    [pw] = [n for n in got.nodes.values() if n.password]
    snap = json.loads((DATA / "shadow.json").read_text())["snap"]
    nodes, strings = snap["documents"][0]["nodes"], snap["strings"]
    tag = {b: strings[nodes["nodeName"][i]] for i, b in enumerate(nodes["backendNodeId"])}
    assert tag[pw.backend] == "INPUT"
    assert {tag[n.backend] for n in got.nodes.values() if n.role == "textbox"} == {"INPUT"}
