"""Shared fixtures for tests/test_settings_*.py: a scratch var/ with its own settings,
the real config/ (read-only), and a small app with the settings and REST routers."""
from __future__ import annotations

import httpx

from celine.settings import files

KEY = "or-test-key-5b1f0c7a9e2d4d61aaaa"

PROVIDERS = {
    "main": {"kind": "llama-swap", "label": "llama-swap",
             "base_url": "http://127.0.0.1:11434/v1",
             "models": {
                 "big": {"label": "Big", "caps": ["chat", "tools"], "slots": 1,
                         "thinking_levels": ["low", "medium", "xhigh"],
                         "params": {"temperature": 0.7, "max_tokens": 4096},
                         "llama": {"chat_template_kwargs": {"enable_thinking": False},
                                   "top_k": 40}},
                 "emb": {"label": "Emb", "caps": ["embeddings"]},
                 "rr": {"label": "Reranker", "caps": ["rerank"]},
                 "eyes": {"label": "Eyes", "caps": ["chat", "vision"]}}},
    "lan": {"kind": "ollama", "label": "gpu-box", "base_url": "http://192.168.1.9:11434/v1",
            "options": {"max_concurrent": 1},
            "models": {"small": {"label": "Small", "caps": ["chat", "tools", "vision"],
                                 "params": {"reasoning_effort": "none"}},
                       "emb8": {"label": "Emb Q8", "caps": ["embeddings"]}}},
    "or": {"kind": "openrouter", "label": "OpenRouter",
           "base_url": "https://openrouter.ai/api/v1",
           "options": {"service_tier": "flex", "order": ["deepinfra"]},
           "models": {"anthropic/claude-x": {"label": "Claude X",
                                             "caps": ["chat", "tools", "vision"],
                                             "reasoning_effort": "medium"}}},
}
ROLES = {
    "chat": {"chain": ["main/big"]},
    "small": {"chain": ["lan/small", "main/big"]},
    "embeddings": {"chain": ["main/emb", "lan/emb8"]},
    "reranker": {"chain": ["main/rr"]},
    "vision": {"chain": ["lan/small", "main/eyes"]},
}


def scratch(tmp_path, monkeypatch, *, providers=PROVIDERS, roles=ROLES, tools=None,
            general=None, write=True):
    """A var/ under tmp_path with settings files, and CELINE_DATA_DIR pointing at it."""
    import celine.settings as st
    st.CONVERSATION_MODELS.clear()
    st.CONVERSATION_THINKING.clear()
    st.CONVERSATION_ROLES.clear()
    var = tmp_path / "var"
    monkeypatch.setenv("CELINE_DATA_DIR", str(var))
    if write:
        root = var / "settings"
        files.write(root, "providers", {"providers": providers})
        files.write(root, "roles", roles)
        files.write(root, "tools", {"defaults": tools or {}})
        files.write(root, "general", general or {"timezone": "Europe/Berlin"})
    return var


def make_app(cfg, store):
    from fastapi import FastAPI

    from celine.errors import install_error_handler
    from celine.api import forms as forms_api, settings as settings_api
    from celine.conversations import api as conversations_api
    from celine.core.bus import EventBus
    from celine.core.services import Services
    from celine.skills import build as build_registry
    app = FastAPI()
    install_error_handler(app)
    app.include_router(conversations_api.router)
    app.include_router(settings_api.router)
    app.include_router(forms_api.router)
    from celine.api import her as her_api
    from celine.guard import api as guard_api
    app.include_router(her_api.router)
    app.include_router(guard_api.router)
    services = Services(cfg=cfg, store=store, bus=EventBus())
    services.registry = build_registry(services)
    app.state.services = services
    return app


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
