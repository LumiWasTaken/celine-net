from types import SimpleNamespace

import pytest

from celine.settings.agent_models import cloud, picked_by_user, provider_cloud


@pytest.mark.parametrize("kind,url,is_cloud", [
    ("openrouter", None, True),                         # its kind's one address
    ("anthropic", None, True),
    ("openai", "https://api.openai.com/v1", True),      # a hosted OpenAI-compatible one
    ("openai", "http://8.8.8.8:8000/v1", True),
    ("llama-server", "https://gpu.example.com/v1", True),
    ("openrouter", "http://127.0.0.1:4000/v1", False),  # a kind with a default, here
    ("llama-server", "http://localhost:8080/v1", False),
    ("llama-server", "http://192.168.1.20:8080/v1", False),
    ("ollama", "http://10.0.0.5:11434/v1", False),
    ("ollama", "http://169.254.3.4:11434/v1", False),
    ("ollama", "http://[fd00::5]:11434/v1", False),
    ("ollama", "http://[::1]:11434/v1", False),
    ("openai", "http://gpu:8080/v1", False),            # one label
    ("openai", "http://models.local:8080/v1", False),
    ("openai", "http://box.lan/v1", False),
    ("openai", "http://box.home.arpa/v1", False),
    ("openai", "http://llm.internal/v1", False),
    ("openai", "http://100.100.1.2:8080/v1", False),    # Tailscale (100.64/10, CGNAT)
    ("ollama", "http://100.64.0.9:11434/v1", False),
])
def test_cloud_is_decided_by_the_providers_host(kind, url, is_cloud):
    p = {"kind": kind, **({"base_url": url} if url else {})}
    s = SimpleNamespace(providers={"p": p}, split=lambda ref: tuple(ref.split("/", 1)))
    assert cloud(s, "p/m") is is_cloud


def test_the_cloud_switch_wins_over_the_address_and_without_it_the_address_says():
    assert provider_cloud({"kind": "openrouter", "cloud": False}) is False, \
        "the user's own proxy under a public name"
    assert provider_cloud({"kind": "openai", "base_url": "http://192.168.1.5/v1",
                           "cloud": True}) is True, "a LAN box that forwards to a cloud"
    p = {"kind": "openai", "base_url": "http://192.168.1.5/v1"}
    assert provider_cloud(p) is False
    p["base_url"] = "https://api.example.com/v1"
    assert provider_cloud(p) is True, "no switch saved: a new address changes it"


def test_a_model_the_user_picked_is_one_in_any_role_or_the_conversations_own():
    s = SimpleNamespace(roles={"chat": {"chain": ["a/x"]}, "writer": {"chain": ["b/y"]},
                               "tasks": {}}, general={})
    assert picked_by_user(s, "a/x", None) and picked_by_user(s, "b/y", None)
    assert picked_by_user(s, "c/z", "c/z"), "the chat's own model"
    assert not picked_by_user(s, "c/z", None) and not picked_by_user(s, "c/z", "a/x")
    plan = {"steps": [{"model": "c/z", "by": "user"}, {"model": "d/w", "by": "agent"},
                      {"model": None, "by": None}]}
    assert picked_by_user(s, "c/z", None, plan), "a step model the user set"
    assert not picked_by_user(s, "d/w", None, plan), "one the agent set"


def test_ask_before_cloud_models_from_your_lists_makes_no_list_a_pick():
    s = SimpleNamespace(roles={"chat": {"chain": ["a/x"]}}, general={"ask_listed_cloud": True})
    assert not picked_by_user(s, "a/x", None), "in a list, it asks"
    assert picked_by_user(s, "a/x", "a/x"), "the chat's own model still never asks"
    plan = {"steps": [{"model": "a/x", "by": "user"}]}
    assert picked_by_user(s, "a/x", None, plan), "nor a step model the user set"
