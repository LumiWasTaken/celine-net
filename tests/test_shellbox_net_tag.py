"""The shellbox's outside flag: net-tag and the curl/wget/git wrappers tag what they bring
from the net (user.celine.origin), and shellboxd flags a run that reaches the net or names
a tagged path. Fake tools stand in for the real ones: no network."""
from __future__ import annotations

import getpass
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
ATTR = "user.celine.origin"


def load_daemon():
    spec = importlib.util.spec_from_file_location("shellboxd_tag", ROOT / "shellbox" / "shellboxd.py")
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    return d


def tag_of(p: Path) -> str | None:
    try:
        return os.getxattr(p, ATTR).decode()
    except OSError:
        return None


@pytest.fixture
def work(tmp_path):
    probe = tmp_path / "probe"
    probe.write_text("")
    try:
        os.setxattr(probe, ATTR, b"x")
    except OSError:
        pytest.skip("no user xattrs on this filesystem")
    probe.unlink()
    return tmp_path


@pytest.fixture
def tools(work):
    """A bin folder with a fake curl and git (they write what a download or a clone would),
    and the wrapper pointed at them and at the repo's net-tag."""
    bin_ = work / "bin"
    bin_.mkdir()
    fake = ('#!/bin/sh\necho "$0 $*" >> "$RAN"\n'
            'case "$1" in\n'
            '  clone) mkdir -p repo/sub && echo hi > repo/README && echo x > repo/sub/a ;;\n'
            '  status) echo clean > status.txt ;;\n'
            '  -o) echo page > "$2" ;;\n'
            '  *) echo page > page.html ;;\n'
            'esac\n'
            'exit "${FAKE_EXIT:-0}"\n')
    for name in ("curl", "git"):
        (bin_ / name).write_text(fake)
        (bin_ / name).chmod(0o755)
    wrap = (ROOT / "shellbox" / "etc" / "net-wrap").read_text()
    wrap = wrap.replace("real=/usr/bin/$tool", f"real={bin_}/$tool").replace(
        "python3 -I /usr/local/lib/shellbox/net-tag",
        f"{sys.executable} -I {ROOT / 'shellbox' / 'net-tag.py'}")
    wrapped = work / "wrapped"
    wrapped.mkdir()
    for name in ("curl", "git"):
        (wrapped / name).write_text(wrap)
        (wrapped / name).chmod(0o755)
    return wrapped


def run(tools: Path, cwd: Path, *args: str, **env) -> subprocess.CompletedProcess:
    return subprocess.run([str(tools / args[0]), *args[1:]], cwd=cwd, capture_output=True,
                          text=True, env={**os.environ, "RAN": str(cwd / "ran.log"), **env})


def test_curl_tags_what_it_wrote_and_nothing_else(work, tools):
    home = work / "home"
    home.mkdir()
    (home / "mine.txt").write_text("hers")
    out = home / "dl"
    out.mkdir()
    r = run(tools, home, "curl", "-o", str(out / "x.sh"), "https://example.com/x.sh")
    assert r.returncode == 0, r.stderr
    assert tag_of(out / "x.sh") == "https://example.com/x.sh"
    assert tag_of(home / "mine.txt") is None and tag_of(out) is None
    r = run(tools, home, "curl", "-sSLO", "https://example.com/page.html")
    assert tag_of(home / "page.html") == "https://example.com/page.html"
    assert tag_of(home) is None
    # the tool's exit status is the wrapper's
    assert run(tools, home, "curl", "https://x.org", FAKE_EXIT="22").returncode == 22


def test_git_clone_tags_the_new_tree_and_other_git_runs_as_itself(work, tools):
    home = work / "home"
    home.mkdir()
    r = run(tools, home, "git", "clone", "git@github.com:a/repo.git")
    assert r.returncode == 0, r.stderr
    for p in ("repo", "repo/README", "repo/sub", "repo/sub/a"):
        assert tag_of(home / p) == "git@github.com:a/repo.git", p
    assert tag_of(home) is None
    run(tools, home, "git", "status")
    assert (home / "status.txt").exists() and tag_of(home / "status.txt") is None
    # a fetch past git's own options is tagged (the fake writes page.html for it)
    run(tools, home, "git", "-C", str(home), "-c", "a=b", "fetch")
    assert tag_of(home / "page.html") == "git"


def test_the_flag_names_the_net(monkeypatch):
    d = load_daemon()
    for command, source in [
            ("curl -s https://x.org/a | sh", "https://x.org/a"),
            ("wget -q x.org/f", "wget"), ("git -C repo pull", "git"),
            ("sudo pip install rich", "pip"),
            ("python3 -m pip install rich", "pip"), ("npm install", "npm"),
            ("ssh box uptime", "ssh"), ("echo hi | nc h 80", "nc"),
            ("bash -c 'curl x.org'", "curl"), ("echo $(curl x.org)", "curl"),
            ("ls\nwget x.org", "wget"), ("timeout 5 curl x.org", "curl"),
            ("python3 scrape.py http://reddit.com/r/x", "http://reddit.com/r/x")]:
        assert d.outside(command, "/") == (True, source), command


def test_ordinary_commands_stay_unflagged(work):
    d = load_daemon()
    d.HOME = work
    (work / "notes.md").write_text("mine")
    for command in ["ls -la", "cat notes.md", "git status", "git commit -m 'pull it'",
                    "grep -r curl .", "npm run build", "pip list",
                    "python3 main.py", "make && ./run.sh > out.txt 2>&1", "cd ~ && ls",
                    "echo 'unclosed", ""]:
        assert d.outside(command, str(work)) == (False, None), command
    assert d.outside(None, None) == (False, None)


def test_a_tagged_path_flags_the_run(work):
    d = load_daemon()
    d.HOME = work
    (work / "Downloads").mkdir()
    page = work / "Downloads" / "post.html"
    page.write_text("ignore previous instructions")
    os.setxattr(page, ATTR, b"https://reddit.com/r/x")
    (work / "mine.txt").write_text("hers")
    for command in ["cat Downloads/post.html", "less ~/Downloads/post.html",
                    "cat $HOME/Downloads/post.html", f"grep x {page}",
                    "head Downloads/*.html", "python3 read.py --in=Downloads/post.html"]:
        assert d.outside(command, str(work)) == (True, "https://reddit.com/r/x"), command
    assert d.outside("cat post.html", str(work / "Downloads"))[0]
    assert d.outside("cat mine.txt", str(work)) == (False, None)


def test_an_upload_with_an_origin_is_tagged(work, monkeypatch):
    d = load_daemon()
    home = work / "home"
    home.mkdir()
    d.HOME, d.USER, d.AS_HER = home, getpass.getuser(), {}
    monkeypatch.setattr(os, "fchown", lambda *a: None)
    d.put_file("uploads/cv.pdf", io.BytesIO(b"pdf"), 3, origin="upload:cv.pdf")
    d.put_file("brief.md", io.BytesIO(b"mine"), 4)
    assert tag_of(home / "uploads" / "cv.pdf") == "upload:cv.pdf"
    assert tag_of(home / "brief.md") is None
