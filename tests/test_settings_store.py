"""var/settings: files, versions, secrets, loading, and what reaches a cloud provider."""
from __future__ import annotations

import os
import stat
import tomllib

import pytest

from celine import boot
from celine.settings import files, secrets, tomlw
from celine.settings.files import Invalid
from celine.settings.registry import backend_config
from settings_fixtures import KEY, PROVIDERS, ROLES, scratch


def test_toml_writer_round_trips_the_settings_shapes():
    data = {"providers": PROVIDERS, "x": {"a.b": [1, 2.5, "q\"uote"], "t": True,
                                          "inline": {"k": {"n": 1}}},
            "list_of_tables": [{"a": 1}, {"a": 2, "b": "ü"}]}
    assert tomllib.loads(tomlw.dumps(data, "header\nline two")) == data


def test_write_is_atomic_and_a_stale_version_is_refused(tmp_path):
    root = tmp_path / "settings"
    files.write(root, "roles", ROLES)
    first = (root / "roles.toml").read_text()
    files.write(root, "roles", ROLES)
    assert (root / "roles.toml").read_text() == first
    assert not list(root.glob(".*.tmp")), "no temp file left behind"
    assert files.version(ROLES) == files.version(tomllib.loads(first))
    with pytest.raises(files.Conflict):
        files.check_version("roles", ROLES, '"0000000000000000"')
    files.check_version("roles", ROLES, f'"{files.version(ROLES)}"')


def test_secrets_are_write_only_mode_600_in_a_700_folder(tmp_path):
    root = tmp_path / "secrets"
    out = secrets.set_key(root, "or", KEY)
    assert out["state"] == "set" and KEY not in repr(out)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "or.key").stat().st_mode) == 0o600
    os.chmod(root / "or.key", 0o644)                 # loosened by hand: tightened on read
    assert secrets.read_key(root, "or") == KEY
    assert stat.S_IMODE((root / "or.key").stat().st_mode) == 0o600
    assert secrets.delete_key(root, "or")["state"] == "not set"
    with pytest.raises(Invalid):
        secrets.set_key(root, "../etc", KEY)
    with pytest.raises(Invalid):
        secrets.set_key(root, "or", "two\nlines")


def test_load_compiles_the_settings_and_the_key_reaches_only_its_backend(tmp_path, monkeypatch):
    var = scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    assert cfg.settings.chain("small") == ["lan/small", "main/big"]
    bc = backend_config(cfg.settings, "main/big")
    assert (bc.base_url, bc.model) == ("http://127.0.0.1:11434/v1", "big")
    assert not [p for p in cfg.problems if "settings" in p.file]
    assert cfg.settings.key_missing("or")
    secrets.set_key(var / "secrets", "or", KEY)
    cfg = boot.load()
    assert backend_config(cfg.settings, "or/anthropic/claude-x").api_key == KEY
    assert KEY in cfg.secrets(), "on the redaction list"
    assert backend_config(cfg.settings, "main/big").api_key is None


def test_nothing_identifying_goes_to_openrouter(tmp_path, monkeypatch):
    from celine.backends.openai import build
    scratch(tmp_path, monkeypatch)
    s = boot.load().settings
    orb = build(backend_config(s, "or/anthropic/claude-x"))
    body = orb.request_body([{"role": "user", "content": "hi"}], [])
    assert body["provider"]["data_collection"] == "deny" and body["provider"]["zdr"] is False
    assert "X-Title" not in orb.headers() and "HTTP-Referer" not in orb.headers()
    s.providers["or"]["models"]["anthropic/claude-x"]["zdr"] = True
    body = build(backend_config(s, "or/anthropic/claude-x")).request_body([], [])
    assert body["provider"]["zdr"] is True
