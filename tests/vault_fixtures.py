"""The vault in the tests: the fake `bw` (fake_bw.py) on PATH, with its state file and the
log of every command line, and an app whose vault is set up through the Settings form's
route, the one way in."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from celine import boot
from settings_fixtures import client, make_app, scratch

CLIENT_ID = "user.0b6f4a2e-test"
CLIENT_SECRET = "fake-client-secret-for-tests-0001"
MASTER = "fake master words for the tests"
FORM = {"vault": {"url": "https://vault.example.com", "client_id": CLIENT_ID,
                  "collection": "celine"},
        "client_secret": CLIENT_SECRET}


class FakeBw:
    def __init__(self, root: Path):
        self.path, self.log = root / "bw.json", root / "bw.log"
        self.write({"url": None, "logged_in": False, "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET, "master": MASTER, "session": "session-1",
                    "reachable": True, "history": True, "syncs": 0, "items": [],
                    "collections": [{"id": "col-1", "organizationId": "org-1",
                                     "name": "celine"}]})

    def state(self) -> dict:
        return json.loads(self.path.read_text())

    def write(self, st: dict) -> None:
        self.path.write_text(json.dumps(st))

    def change(self, **kw) -> None:
        self.write({**self.state(), **kw})

    def calls(self) -> list[list[str]]:
        return [json.loads(ln) for ln in self.log.read_text().splitlines()] \
            if self.log.exists() else []


@pytest.fixture
def fake_bw(tmp_path, monkeypatch) -> FakeBw:
    root = tmp_path / "bw"
    (root / "bin").mkdir(parents=True)
    exe = root / "bin" / "bw"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{Path(__file__).with_name("fake_bw.py")}" "$@"\n')
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{root / 'bin'}:{os.environ['PATH']}")
    fake = FakeBw(root)
    monkeypatch.setenv("FAKE_BW", str(fake.path))
    monkeypatch.setenv("FAKE_BW_LOG", str(fake.log))
    yield fake
    from celine import errors
    errors.set_secrets([], "vault")


@pytest.fixture
async def vault_app(tmp_path, monkeypatch, store, fake_bw):
    """An app with the vault set up and unlocked, and a user who approves every tap
    (.taps: what was asked): its http client, with .cfg, .app, .vault (vault.Vault),
    .skill (vault/skill.py) and .bw (FakeBw)."""
    from celine.vault import Vault
    from celine.approvals import Approvals
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    vault = app.state.services.vault = Vault(cfg, store.emit_global)
    approvals = app.state.services.approvals = Approvals(cfg)
    vault.forget = lambda: approvals.forget("vault")
    taps: list[tuple[str, list[str]]] = []

    async def yes(action, facts, conversation, *_, **__):
        taps.append((action, facts))
        return None
    monkeypatch.setattr(approvals, "ask", yes)
    async with client(app) as c:
        r = await c.put("/api/vault", json=FORM)
        assert r.status_code == 200 and r.json()["status"].startswith("Locked"), r.text
        r = await c.post("/api/vault/unlock", json={"master_password": MASTER})
        assert r.status_code == 200 and r.json()["status"] == "Unlocked", r.text
        c.cfg, c.app, c.bw, c.taps = cfg, app, fake_bw, taps
        c.vault = app.state.services.vault
        c.skill = app.state.services.registry.skills["vault"]
        yield c


def ctx_for(store, cid: str = "", call_id: str = "c1"):
    return SimpleNamespace(conversation_id=cid, turn_id="t1", tool_call_id=call_id,
                           store=store, max_output_tokens=None)


def by_hand(*names: str) -> list[dict]:
    """Entries the user made in a Bitwarden app, named as people name things."""
    return [{"type": 1, "id": f"id-{i}", "name": n, "passwordHistory": None,
             "organizationId": "org-1", "collectionIds": ["col-1"],
             "login": {"username": f"user{i}", "password": f"test-hand-made-{i:04}",
                       "uris": [{"uri": "https://bank.example"}]}} for i, n in enumerate(names)]
