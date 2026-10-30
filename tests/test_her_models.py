from types import SimpleNamespace

import pytest

from celine.settings.her_models import cloud


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
])
def test_cloud_is_decided_by_the_providers_host(kind, url, is_cloud):
    p = {"kind": kind, **({"base_url": url} if url else {})}
    s = SimpleNamespace(providers={"p": p}, split=lambda ref: tuple(ref.split("/", 1)))
    assert cloud(s, "p/m") is is_cloud
