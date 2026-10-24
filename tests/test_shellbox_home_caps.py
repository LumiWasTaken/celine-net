"""The shellbox's home caps (SHELLBOX_HOME_GB the hard one, SHELLBOX_HOME_SOFT_GB the
soft one): past the soft cap its next result says so, once per measure; past the hard
cap the daemon takes no upload. The measure is handed in; no du runs."""
from __future__ import annotations

import getpass
import importlib.util
import io
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
GB = 1024 ** 3


@pytest.fixture
def d(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("shellboxd_caps",
                                                  ROOT / "shellbox" / "shellboxd.py")
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    home = tmp_path / "home"
    home.mkdir()
    d.HOME, d.USER, d.AS_HER = home, getpass.getuser(), {}
    monkeypatch.setattr(os, "fchown", lambda *a: None)
    monkeypatch.setattr(d, "USAGE", tmp_path / "usage")
    return d


def put(d, n: int):
    return d.put_file(f"f{n}.bin", io.BytesIO(b"x" * n), n)


def test_past_the_soft_cap_it_is_told_and_past_the_hard_cap_no_upload_lands(d):
    d.HOME_GB, d.HOME_SOFT_GB = "1", "0.5"
    assert d.caps() == (GB // 2, GB)
    d.measured(GB // 4, at=1)
    assert d.home_note() == []

    d.measured(GB // 2 + 1, at=2)
    assert d.home_note() == [f"home_over:{GB // 2 + 1}:{GB // 2}"]
    assert d.home_note() == [], "once per measure"
    assert put(d, 4)["bytes"] == 4, "the soft cap only warns"

    d.measured(GB + 1, at=3)
    assert d.home_note() == [f"home_over:{GB + 1}:{GB}"], "past both: the hard cap"
    with pytest.raises(d.Problem) as e:
        put(d, 3)
    assert e.value.status == 413 and "1 GB" in str(e.value)
    assert not (d.HOME / "f3.bin").exists()

    d.measured(GB - 10, at=4)
    put(d, 6)
    with pytest.raises(d.Problem):
        put(d, 8)                       # what landed since the measure counts


def test_the_soft_cap_defaults_below_the_hard_one_and_no_size_is_no_cap(d):
    d.HOME_GB, d.HOME_SOFT_GB = "20", ""
    assert d.caps() == (18 * GB, 20 * GB)
    d.HOME_GB = ""
    assert d.caps() is None
    d.measured(500 * GB, at=1)
    assert d.home_note() == [] and put(d, 3)["bytes"] == 3
