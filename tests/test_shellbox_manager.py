"""The shellbox's morning reset (shellbox-manager/manager.sh): the snapshot of its home
before it, from a named volume or a host folder (the home disk), into the folder beside it.
A fake docker stands in for the real one: it answers the inspects,
logs the calls and runs the snapshot's script on the host with its mounts as symlinks."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
DAY = "2026-10-23"

FAKE_DOCKER = r'''
import os, subprocess, sys
from pathlib import Path
a = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(" ".join(a) + "\n")
if a[0] == "inspect":
    t = a[a.index("-f") + 1]
    print("celine" if "compose.project" in t else "img" if ".Config.Image" in t
          else os.environ["FAKE_MOUNT"])
elif a[0] == "compose" and "ps" in a:
    print("box")
elif a[0] == "run":
    root = Path(os.environ["FAKE_ROOT"])
    for i, x in enumerate(a):
        if x == "-v":
            src, dst = a[i + 1].split(":")[:2]
            if not src.startswith("/"):                  # a named volume
                src = os.environ["FAKE_VOLUMES"] + "/" + src
            Path(src).mkdir(parents=True, exist_ok=True)  # as docker -v does
            link = Path(str(root) + dst)
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(src)
    i = a.index("-c")
    script = a[i + 1].replace("/data/", str(root) + "/data/")
    sys.exit(subprocess.call(["sh", "-c", script] + a[i + 2:]))
'''
FAKE_DATE = f'''
import sys
from datetime import datetime, timezone
now = datetime(2026, 10, 23, 4, 0, tzinfo=timezone.utc)
a = sys.argv[1:]
if a == ["+%F"]:
    print("{DAY}")
elif a == ["+%s"]:
    print(int(now.timestamp()))
elif a[:1] == ["-d"] and a[1].startswith("@") and a[2:] == ["+%F"]:
    print(datetime.fromtimestamp(int(a[1][1:]), timezone.utc).strftime("%F"))
else:
    print("{DAY} 04:00")
'''


def run(tmp_path: Path, mount: str, **env: str) -> tuple[str, str]:
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "docker").write_text(f"#!{sys.executable}\n{FAKE_DOCKER}")
    (bin_ / "date").write_text(f"#!{sys.executable}\n{FAKE_DATE}")
    for p in bin_.iterdir():
        p.chmod(0o755)
    root = tmp_path / "root"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir()
    log = tmp_path / "docker.log"
    log.write_text("")
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "FAKE_LOG": str(log),
           "FAKE_MOUNT": mount, "FAKE_ROOT": str(root),
           "FAKE_VOLUMES": str(tmp_path / "volumes"), "SHELLBOX_SNAPSHOTS": "", **env}
    out = subprocess.run(["sh", str(ROOT / "shellbox-manager" / "manager.sh"), "reset"],
                         env=env, capture_output=True, text=True, check=True).stdout
    return out, log.read_text()


@pytest.fixture(autouse=True)
def needs_rsync():
    if not shutil.which("rsync"):
        pytest.skip("rsync")


def home(at: Path) -> Path:
    at.mkdir(parents=True)
    (at / "same.txt").write_text("kept\n")
    (at / "new.txt").write_text("today\n")
    return at


def test_a_host_folder_home_is_kept_next_to_it_with_the_last_snapshot_hardlinked(tmp_path):
    src = home(tmp_path / "service" / "shellbox")
    old = tmp_path / "service" / "shellbox-snapshots" / "2026-10-22"
    old.mkdir(parents=True)
    (old / "same.txt").write_text("kept\n")
    (old / "gone.txt").write_text("deleted since\n")
    os.utime(old / "same.txt", ns=(os.stat(src / "same.txt").st_mtime_ns,) * 2)
    out, log = run(tmp_path, f"|{src}")
    assert "home snapshot kept" in out
    day = tmp_path / "service" / "shellbox-snapshots" / DAY
    assert sorted(p.name for p in day.iterdir()) == ["new.txt", "same.txt"]
    assert (day / "same.txt").stat().st_ino == (old / "same.txt").stat().st_ino, \
        "a file that did not change costs nothing"
    assert (old / "gone.txt").exists(), "the snapshot before stays as it was"
    assert f"-v {src}:/data/shellbox:ro" in log
    assert "up -d --force-recreate --no-deps shellbox" in log


def test_a_named_volume_home_is_kept_in_the_same_volume(tmp_path):
    home(tmp_path / "volumes" / "celine_data" / "shellbox")
    out, log = run(tmp_path, "celine_data|/var/lib/docker/volumes/celine_data/_data")
    assert "home snapshot kept" in out
    day = tmp_path / "volumes" / "celine_data" / "shellbox-snapshots" / DAY
    assert (day / "new.txt").read_text() == "today\n"
    assert "-v celine_data:/data " in log


def test_no_home_mount_fails_the_snapshot_and_the_reset_goes_on(tmp_path):
    out, log = run(tmp_path, "")
    assert "home snapshot failed, the reset goes on" in out
    assert " run " not in f" {log}"
    assert "up -d --force-recreate --no-deps shellbox" in log


def test_snapshots_older_than_the_days_kept_go(tmp_path):
    home(tmp_path / "volumes" / "celine_data" / "shellbox")
    snaps = tmp_path / "volumes" / "celine_data" / "shellbox-snapshots"
    for day in ("2026-10-10", "2026-10-15", "2026-10-16", "2026-10-22"):
        (snaps / day).mkdir(parents=True)
        (snaps / day / "same.txt").write_text("kept\n")
    out, _ = run(tmp_path, "celine_data|/x", SHELLBOX_SNAPSHOT_KEEP_DAYS="7")
    assert "home snapshot kept" in out
    assert sorted(p.name for p in snaps.iterdir()) == ["2026-10-16", "2026-10-22", DAY]

    run(tmp_path, "celine_data|/x", SHELLBOX_SNAPSHOT_KEEP_DAYS="0")
    assert len(list(snaps.iterdir())) == 3, "0 keeps every one"
