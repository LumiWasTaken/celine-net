"""errors.said: the words of any server's error answer, in whatever shape it sends them."""
import json

import pytest

from celine import errors
from celine.errors import SAID_CAP, said

LLAMA = {"error": {"code": 400, "message": "the request exceeds the available context size",
                   "type": "exceed_context_size_error", "n_ctx": 4096, "n_prompt_tokens": 5000}}

CASES = [
    ("openai shape", {"error": {"message": "The model server is paused. Retry later.",
                                "type": "model_paused", "code": 503}},
     "The model server is paused. Retry later.", "model_paused"),
    ("anthropic", {"type": "error", "error": {"type": "overloaded_error",
                                              "message": "Overloaded"}},
     "Overloaded", "overloaded_error"),
    ("plain error string", {"error": "model requires more system memory"},
     "model requires more system memory", ""),
    ("ollama wrapping llama.cpp", {"error": json.dumps(LLAMA)},
     "the request exceeds the available context size", "exceed_context_size_error"),
    ("fastapi detail", {"detail": "Model is loading"}, "Model is loading", ""),
    ("pydantic detail list", {"detail": [{"loc": ["body", "messages"], "msg": "field required",
                                          "type": "missing"}]},
     "body.messages: field required", "missing"),
    ("problem details", {"type": "about:blank", "title": "Service Unavailable",
                         "detail": "maintenance until 06:00", "status": 503},
     "Service Unavailable: maintenance until 06:00", "about:blank"),
    ("graphql errors", {"errors": [{"message": "a"}, {"message": "b"}]}, "a; b", ""),
    ("oauth", {"error": "invalid_client", "error_description": "bad secret"}, "bad secret", ""),
    ("an odd key deep down", {"weird": {"nested": {"failure_reason": "disk full"}}},
     "disk full", ""),
    ("json without words", {"ok": False}, '{"ok": false}', ""),
]


@pytest.mark.parametrize("name,body,text,kind", CASES, ids=[c[0] for c in CASES])
def test_a_json_error_gives_its_words_and_kind(name, body, text, kind):
    s = said(json.dumps(body), 500)
    assert (s.text, s.kind) == (text, kind)


def test_the_error_object_keeps_the_numbers_a_caller_reads():
    for body in (LLAMA, {"error": json.dumps(LLAMA)}):
        obj = said(json.dumps(body)).obj
        assert (obj["n_ctx"], obj["n_prompt_tokens"]) == (4096, 5000)


def test_html_text_and_empty_bodies_still_say_something():
    page = ("<html><head><title>502 Bad Gateway</title><style>b{}</style></head>"
            "<body><h1>502 Bad Gateway</h1><hr>nginx</body></html>")
    assert said(page, 502).text == "502 Bad Gateway"
    assert said("<html><body><p>Bad &amp; gone</p></body></html>", 502).text == "Bad & gone"
    assert said("go away\n  now", 418).text == "go away now"
    assert said("", 503).text == "Service Unavailable"
    assert said(b"", None).text == ""
    assert said('"a json string"', 500).text == "a json string"
    assert said("{}", 401).text == "Unauthorized", "an empty object says nothing"


def test_long_words_keep_their_start_and_end():
    text = said("start " + "x" * 2000 + " the cause").text
    assert len(text) <= SAID_CAP
    assert text.startswith("start ") and text.endswith(" the cause") and "…" in text


def test_known_secrets_and_token_shapes_are_masked(monkeypatch):
    monkeypatch.setattr(errors, "_SECRETS", ["hunter2-long-secret"])
    s = said(json.dumps({"error": {"message":
                                   "key sk-abcdefghijklmnopqrstuvwxyz and hunter2-long-secret"}}))
    assert s.text == "key **** and ****"


def test_answered_wraps_the_words_in_one_sentence():
    import httpx
    assert errors.answered("ComfyUI at c:8188", httpx.Response(503, json={
        "error": {"message": "out of memory"}})) == "ComfyUI at c:8188 returned 503: out of memory"
    assert errors.answered("X", httpx.Response(502)) == "X returned 502: Bad Gateway"
    assert errors.answered("X", httpx.Response(599)) == "X returned 599."
