"""net-tag: runs curl, wget or git as itself, then tags every file the run wrote with
where it came from, the extended attribute user.celine.origin=<url>. shellboxd reads the
tag after its runs: a command that names a tagged path reads text from the net.

    net-tag <the real tool> <its arguments>...

/usr/local/bin/curl, wget and git (etc/net-wrap) start it. What the run wrote: files
changed since it began in the folder it ran in, in every path its arguments name, and in
the folders below those that changed; folders that are new at that top level (a clone)
are tagged too. Tagging never fails the run; the tool's exit status is net-tag's.
"""
from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import sys
import time

ATTR = "user.celine.origin"
URL = re.compile(r"^[a-z][a-z0-9+.-]*://\S|^[\w.-]+@[\w.-]+:")    # https://x, git@host:x
SKIP = ("/proc/", "/sys/", "/dev/", "/run/")
# the clock file times come from (Linux CLOCK_REALTIME_COARSE): a file written right after
# the start may carry a time a little before a fine clock's start
COARSE = 5


def origin(real: str, args: list[str], cwd: str | None) -> str:
    """The first URL among the arguments; for git without one, its default remote."""
    for a in args:
        v = a.split("=", 1)[-1] if a.startswith("--") else a
        if URL.match(v):
            return v
    if os.path.basename(real) == "git":
        try:
            r = subprocess.run([real, "ls-remote", "--get-url"], cwd=cwd, text=True,
                               capture_output=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return os.path.basename(real)


def roots(args: list[str]) -> list[str]:
    """The folder the run is in, and every argument (or --opt=value) that is a path."""
    found = [os.getcwd()]
    for a in args:
        for v in {a, a.split("=", 1)[-1]}:
            if v and not v.startswith("-") and os.path.lexists(v):
                found.append(os.path.abspath(v))
    return found


def entries(path: str) -> set[str]:
    try:
        return {e.name for e in os.scandir(path)}
    except OSError:
        return set()


def tag(path: str, value: bytes) -> None:
    try:
        os.setxattr(path, ATTR, value, follow_symlinks=False)
    except OSError:                    # not its own, or a filesystem without user xattrs
        pass


def tag_written(before: dict[str, set[str]], args: list[str], since: int, value: bytes) -> None:
    # note: "changed since the start" also tags what another program wrote meanwhile in
    # those folders; snapshot whole trees if that ever matters
    todo = [(r, r not in before and os.path.isdir(r), True) for r in roots(args)]
    seen: set[str] = set()
    while todo:
        path, new, top = todo.pop()
        if path in seen or (path + "/").startswith(SKIP):
            continue
        seen.add(path)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            if st.st_ctime_ns >= since:
                tag(path, value)
        elif stat.S_ISDIR(st.st_mode):
            if new:
                tag(path, value)
            if new or top or st.st_ctime_ns >= since:
                old = before.get(path, set()) if top else set()
                todo += [(os.path.join(path, n), new or (top and n not in old), False)
                         for n in entries(path)]


def main() -> int:
    real, args = sys.argv[1], sys.argv[2:]
    before = {r: entries(r) for r in roots(args) if os.path.isdir(r)}
    since = time.clock_gettime_ns(COARSE)
    # Ctrl-C reaches the tool from the terminal itself; net-tag stays to tag what it wrote
    signal.signal(signal.SIGINT, lambda *_: None)
    signal.signal(signal.SIGQUIT, lambda *_: None)
    try:
        p = subprocess.Popen([real, *args])
    except OSError as e:
        print(f"{os.path.basename(real)}: {e.strerror}", file=sys.stderr)
        return 127
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, lambda n, _: p.send_signal(n))
    rc = p.wait()
    cwd = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "-C"), None)
    tag_written(before, args, since, origin(real, args, cwd).encode()[:4000])
    return 128 - rc if rc < 0 else rc


if __name__ == "__main__":
    sys.exit(main())
