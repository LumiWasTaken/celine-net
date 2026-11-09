"""A model provider's error in its own words, the same from a probe and from a request,
and a Retry-After given as an HTTP date read as the seconds left."""
from __future__ import annotations

import json
from email.utils import format_datetime
from datetime import datetime, timezone

import httpx
import pytest

from celine.backends.openai import http_error, retry_seconds
from celine.backends.router import Router
from celine.boot import load
from settings_fixtures import scratch

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc).timestamp()


def test_a_retry_after_date_reads_as_the_seconds_until_it():
    at = format_datetime(datetime(2026, 11, 8, 3, 0, 45, tzinfo=timezone.utc), usegmt=True)
    assert retry_seconds(at, now=NOW) == 45.0
    assert retry_seconds("12", now=NOW) == 12.0
    assert retry_seconds("Wed, 21 Oct 2015 07:28:00 GMT", now=NOW) == 0.0, "a past date"
    assert retry_seconds("soon", now=NOW) is None and retry_seconds(None) is None


def test_a_busy_answer_keeps_its_retry_after_date_on_the_error(monkeypatch):
    monkeypatch.setattr("celine.backends.openai.time.time", lambda: NOW)
    at = format_datetime(datetime(2026, 11, 8, 3, 2, tzinfo=timezone.utc), usegmt=True)
    e = http_error("5090", "qwen", 503, "", at)
    assert e.retry_after == 120.0 and str(e) == "5090 returned 503: Service Unavailable"


async def test_a_probe_and_a_request_word_a_502_the_same(tmp_path, monkeypatch):
    page = "<html><head><title>502 Bad Gateway</title></head><body>nginx</body></html>"
    t = httpx.MockTransport(lambda r: httpx.Response(502, text=page))
    scratch(tmp_path, monkeypatch, providers={"gpu": {
        "kind": "llama-swap", "label": "5090", "base_url": "http://gpu:8080/v1",
        "models": {"qwen": {"caps": ["chat"]}}}}, roles={})
    st = await Router(load(), None, transport=t).probe("gpu", max_age=0)
    assert st.error == str(http_error("5090", "qwen", 502, page)) == \
        "5090 returned 502: 502 Bad Gateway"


def test_a_401_names_the_key_fix_after_the_servers_words():
    e = http_error("5090", "qwen", 401, json.dumps({"error": {"message": "bad key"}}))
    assert str(e) == "5090 returned 401: bad key. Check the key in Settings, Models."
