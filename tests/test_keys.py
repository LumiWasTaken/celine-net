"""The stack's own keys (server/celine/keys.py): made once, read by the app, derived
files for the other containers."""
from __future__ import annotations

import hashlib
import stat

from celine import keys
from celine.boot import load


def _mode(p) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_generation_is_idempotent_and_the_derived_files_follow_the_keys(tmp_path):
    d = tmp_path / "data"
    made = keys.ensure(d)
    assert set(made) == {f"{n}.key" for n in keys.NAMES} | {
        "shell.hash", "browser.hash", "connector-clients", "searxng-settings.yml"}
    first = {p.name: p.read_text() for p in (d / "keys").iterdir()}
    assert keys.ensure(d) == []
    assert {p.name: p.read_text() for p in (d / "keys").iterdir()} == first

    shell = keys.read(d, "shell")
    assert len(shell) == 48 and shell == first["shell.key"].strip()
    assert first["shell.hash"].strip() == hashlib.sha256(shell.encode()).hexdigest()
    assert first["browser.hash"].strip() == keys.sha256(keys.read(d, "browser"))
    assert first["connector-clients"] == f"live:{keys.read(d, 'connector')}:owner\n"
    assert f'  secret_key: "{keys.read(d, "searxng")}"\n' in first["searxng-settings.yml"]
    assert "use_default_settings: true" in first["searxng-settings.yml"]
    assert _mode(d / "keys") == 0o700 and _mode(d / "keys" / "shell.key") == 0o600
    assert _mode(d / "keys" / "connector-clients") == 0o600
    assert _mode(d / "keys" / "shell.hash") == _mode(d / "keys" / "searxng-settings.yml") == 0o644

    # a replaced key: the hash follows, the others stay
    (d / "keys" / "shell.key").write_text("k" * 24 + "\n")
    assert keys.ensure(d) == ["shell.hash"]
    assert (d / "keys" / "shell.hash").read_text().strip() == keys.sha256("k" * 24)
    assert keys.read(d, "browser") == first["browser.key"].strip()


def test_the_readers_find_the_key(tmp_path, stack_key, monkeypatch):
    """Startup makes the keys in the data dir; the connector header, the shellbox and
    browser clients and the model proxy read them from there."""
    from celine import shellbox
    from celine.api import llm_proxy
    from celine.browser import sync as browser_sync
    monkeypatch.delenv("CONNECTOR_KEY", raising=False)
    stack_key("shell", "s" * 24)
    stack_key("browser", "b" * 24)
    stack_key("shellbox-llm", "l" * 24)
    cfg = load()
    connector = (tmp_path / "var" / "keys" / "connector.key").read_text().strip()
    assert cfg.mcp["discord"].headers["Authorization"] == f"Bearer {connector}"
    assert cfg.mcp["whatsapp"].args[-1] == str(tmp_path / "var" / "keys" / "connector-clients")
    assert shellbox.client(cfg).headers["authorization"] == "Bearer " + "s" * 24
    assert browser_sync._client(cfg).headers["authorization"] == "Bearer " + "b" * 24
    assert llm_proxy.key(cfg) == "l" * 24


def test_the_environment_still_wins_for_the_connector_key(monkeypatch):
    monkeypatch.setenv("CONNECTOR_KEY", "from-the-environment-1234")
    assert load().mcp["discord"].headers["Authorization"] == "Bearer from-the-environment-1234"
