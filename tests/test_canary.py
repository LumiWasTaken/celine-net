"""Redaction canary: a key typed into Settings never reaches a log line or an error body."""
from __future__ import annotations

import logging

from celine.boot import load
from celine.errors import CelineError, redact, set_secrets
from celine.logs import setup

KEY = "or-canary-9f8e7d6c5b4a3210zzzz"


def test_a_key_typed_into_settings_is_masked_everywhere(tmp_path, monkeypatch):
    from celine.settings import files
    from celine.settings.registry import backend_config
    from celine.settings.secrets import set_key
    monkeypatch.setenv("CELINE_DATA_DIR", str(tmp_path / "var"))
    files.write(tmp_path / "var" / "settings", "providers", {"providers": {"openrouter": {
        "kind": "openrouter", "base_url": "", "models": {"a/b": {"caps": ["chat"]}}}}})
    set_key(tmp_path / "var" / "secrets", "openrouter", KEY)
    cfg = load()
    assert KEY in cfg.secrets()
    assert backend_config(cfg.settings, "openrouter/a/b").api_key == KEY
    set_secrets(cfg.secrets())
    try:
        setup(tmp_path, dev=False)
        log = logging.getLogger("celine.canary")
        log.info("plain %s", KEY)
        log.warning("in extra", extra={"event": "canary", "header": f"Bearer {KEY}"})
        try:
            raise RuntimeError(f"upstream said {KEY}")
        except RuntimeError:
            log.exception("raised")
        for h in logging.getLogger().handlers:
            h.flush()
        text = (tmp_path / "celine.log").read_text()
        assert "canary" in text and KEY not in text
        assert KEY not in str(CelineError("x", f"key {KEY}"))
        assert KEY not in str(CelineError("x", "y", detail={"h": KEY}).as_dict())
        assert redact(f"sk-{'a' * 40}") == "****"
    finally:
        set_secrets([])
        for h in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(h)
