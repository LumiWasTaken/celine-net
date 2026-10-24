"""Speech is opt-in, person-only, bounded, and keeps provider secrets on the server."""
from __future__ import annotations

import json
import tomllib

import httpx
import pytest

from celine import boot, speech
from celine.api import speech as api_mod
from celine.settings import features, secrets
from celine.settings.speech import validate
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio
KEY = "speech-private-test-value"
ENDPOINT = {"url": "https://speech.example/v1", "model": "speech-model"}


@pytest.fixture
async def api(tmp_path, monkeypatch, store):
    var = scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    app.include_router(api_mod.router)
    async with client(app) as c:
        c.cfg, c.var = cfg, var
        yield c


async def setup(api, direction="output", key=KEY):
    r = await api.put(f"/api/speech/{direction}", json={"endpoint": ENDPOINT, "key": key})
    assert r.status_code == 200, r.text
    return r


def upstream(monkeypatch, handler):
    constructor = httpx.AsyncClient
    monkeypatch.setattr(speech, "client", lambda: constructor(transport=httpx.MockTransport(handler), follow_redirects=False))


async def test_default_off_and_no_provider_requests(api, monkeypatch):
    upstream(monkeypatch, lambda r: pytest.fail("off speech made a request"))
    assert not features.on(api.cfg, "speech_input")
    assert not features.on(api.cfg, "speech_output")
    assert (await api.post("/api/speech/audio", json={"text": "Hi"})).status_code == 409
    assert (await api.post("/api/transcriptions", files={"file": ("x.webm", b"x", "audio/webm")})).status_code == 409


async def test_secret_roundtrip_blank_preserves_explicit_clear_and_url_guard(api):
    r = await setup(api)
    assert KEY not in r.text
    saved = await api.get("/api/speech/output")
    assert KEY not in saved.text and saved.json()["key"]["state"] == "set"
    version = saved.json()["version"]
    headers = {"If-Match": version}
    stale = await api.put("/api/speech/output", json={"endpoint": ENDPOINT}, headers={"If-Match": '"old"'})
    assert stale.status_code == 412
    assert (await api.put("/api/speech/output", json={"endpoint": ENDPOINT, "key": ""}, headers=headers)).status_code == 200
    assert secrets.read_key(api.cfg.settings.secrets_root, "speech-output") == KEY
    changed = {**ENDPOINT, "url": "https://other.example/v1"}
    assert (await api.put("/api/speech/output", json={"endpoint": changed})).status_code == 400
    assert (await api.put("/api/speech/output", json={"endpoint": changed, "key": None})).status_code == 200
    assert secrets.read_key(api.cfg.settings.secrets_root, "speech-output") is None
    data = tomllib.loads((api.var / "settings/speech.toml").read_text())
    assert data["output"] == changed
    assert KEY not in json.dumps(data)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:pass@example.org", "https://example.org?q=key", "https://example.org/#token", "https://exa mple.org", "https://[broken", "https://example.org:wrong"])
async def test_bad_urls(api, url):
    assert (await api.put("/api/speech/input", json={"endpoint": {"url": url}})).status_code == 400


async def test_person_only_configuration_and_generation(api):
    await setup(api)
    headers = {"Authorization": "Bearer untrusted"}
    for method, path in [("GET", "/api/speech/output"), ("PUT", "/api/speech/output"),
                         ("GET", "/api/speech/output/voices"), ("POST", "/api/speech/audio")]:
        r = await api.request(method, path, headers=headers, json={"endpoint": ENDPOINT, "text": "hello"})
        assert r.status_code == 403, r.text
    r = await api.post("/api/transcriptions", headers=headers, files={"file": ("x.webm", b"x", "audio/webm")})
    assert r.status_code == 403


async def test_voices_only_tests_discovery_not_enabling_features(api, monkeypatch):
    await setup(api)
    calls = []
    def handle(r):
        calls.append(r)
        assert str(r.url) == ENDPOINT["url"] + "/voices"
        assert r.headers["authorization"] == "Bearer " + KEY
        return httpx.Response(200, json={"voices": ["voice-a", {"id": "voice-b"}, "voice-a"]})
    upstream(monkeypatch, handle)
    r = await api.get("/api/speech/output/voices")
    assert r.json() == {"voices": ["voice-a", "voice-b"]}
    assert len(calls) == 1 and not features.on(api.cfg, "speech_output")


async def test_transcription_forwarding_and_text_only_result(api, monkeypatch):
    await setup(api, "input")
    api.cfg.settings.features["speech_input"] = True
    def handle(r):
        assert r.url.path == "/v1/audio/transcriptions"
        assert r.headers["authorization"] == "Bearer " + KEY
        assert b"speech-model" in r.content and b"audio-bytes" in r.content and b"en" in r.content
        return httpx.Response(200, json={"text": " hello ", "private": "not forwarded"})
    upstream(monkeypatch, handle)
    r = await api.post("/api/transcriptions", files={"file": ("voice.webm", b"audio-bytes", "audio/webm;codecs=opus")}, data={"language": "en"})
    assert r.status_code == 200 and r.json() == {"text": "hello"}


async def test_audio_forwarding_and_limits(api, monkeypatch):
    await setup(api)
    api.cfg.settings.features["speech_output"] = True
    calls = []
    def handle(r):
        calls.append(r)
        assert r.url.path == "/v1/audio/speech"
        assert json.loads(r.content) == {"model": "speech-model", "input": "Hi", "voice": "voice-a", "response_format": "wav"}
        return httpx.Response(200, content=b"wave", headers={"content-type": "audio/wav"})
    upstream(monkeypatch, handle)
    r = await api.post("/api/speech/audio", json={"text": "Hi", "voice": "voice-a"})
    assert r.status_code == 200 and r.content == b"wave" and r.headers["cache-control"] == "no-store"
    for text in ["", " " * 5, "x" * 901, None]:
        assert (await api.post("/api/speech/audio", json={"text": text})).status_code == 400
    assert len(calls) == 1


@pytest.mark.parametrize("status", [302, 401, 500])
async def test_redirects_and_private_provider_errors_not_forwarded(api, monkeypatch, status):
    await setup(api)
    calls = []
    def handle(r):
        calls.append(r)
        return httpx.Response(status, text=KEY, headers={"location": "https://untrusted.example"})
    upstream(monkeypatch, handle)
    r = await api.get("/api/speech/output/voices")
    assert r.status_code == 502 and KEY not in r.text and len(calls) == 1


async def test_bad_provider_content_and_bounded_responses(api, monkeypatch):
    await setup(api)
    api.cfg.settings.features["speech_output"] = True
    upstream(monkeypatch, lambda r: httpx.Response(200, json={"error": KEY}))
    r = await api.post("/api/speech/audio", json={"text": "Hi"})
    assert r.status_code == 502 and KEY not in r.text
    monkeypatch.setattr(speech, "MAX_AUDIO", 3)
    upstream(monkeypatch, lambda r: httpx.Response(200, content=b"oversized", headers={"content-type": "audio/wav"}))
    assert (await api.post("/api/speech/audio", json={"text": "Hi"})).status_code == 502


async def test_invalid_upload_never_calls_provider(api, monkeypatch):
    await setup(api, "input")
    api.cfg.settings.features["speech_input"] = True
    upstream(monkeypatch, lambda r: pytest.fail("invalid upload forwarded"))
    for data, mime in [(b"", "audio/webm"), (b"x", "text/plain")]:
        r = await api.post("/api/transcriptions", files={"file": ("x", data, mime)})
        assert r.status_code == 400


def test_unknown_config_and_whitespace():
    from celine.settings.files import Invalid
    with pytest.raises(Invalid):
        validate({"input": {"api_key": KEY}})
    assert validate({"input": {"url": " https://speech.example/v1/ ", "model": " whisper "}}) == {"input": {"url": "https://speech.example/v1", "model": "whisper"}}


async def test_failed_settings_commit_restores_previous_key(api, monkeypatch):
    from celine.errors import CelineError
    await setup(api)
    async def fail(*args, **kwargs):
        raise CelineError("unavailable", "Save failed.", 503)
    monkeypatch.setattr(api_mod.reload, "commit", fail)
    for key in ["new-private-value", None]:
        r = await api.put("/api/speech/output", json={"endpoint": ENDPOINT, "key": key})
        assert r.status_code == 503
        assert secrets.read_key(api.cfg.settings.secrets_root, "speech-output") == KEY
        assert api.cfg.settings.speech["output"] == ENDPOINT


async def test_input_output_are_independent_and_saved_settings_reload(api):
    await setup(api, "input", "input-private-key")
    await setup(api, "output", "output-private-key")
    reloaded = boot.load().settings
    assert reloaded.speech == {"input": ENDPOINT, "output": ENDPOINT}
    assert secrets.read_key(reloaded.secrets_root, "speech-input") == "input-private-key"
    assert secrets.read_key(reloaded.secrets_root, "speech-output") == "output-private-key"
    assert not features.on(api.cfg, "speech_input") and not features.on(api.cfg, "speech_output")


async def test_partial_commit_never_restores_old_key_at_new_address(api, monkeypatch):
    from celine.errors import CelineError
    await setup(api)
    changed = {**ENDPOINT, "url": "https://changed.example/v1"}
    async def fail_after_write(services, changes):
        api_mod.write.save(services.cfg, changes)
        raise CelineError("unavailable", "Apply failed.", 503)
    monkeypatch.setattr(api_mod.reload, "commit", fail_after_write)
    r = await api.put("/api/speech/output", json={"endpoint": changed, "key": "replacement-private-key"})
    assert r.status_code == 503
    assert boot.load().settings.speech["output"] == changed
    assert secrets.read_key(api.cfg.settings.secrets_root, "speech-output") is None


async def test_oversized_recording_and_bad_language_do_not_reach_provider(api, monkeypatch):
    await setup(api, "input")
    api.cfg.settings.features["speech_input"] = True
    monkeypatch.setattr(speech, "MAX_RECORDING", 3)
    upstream(monkeypatch, lambda r: pytest.fail("invalid recording forwarded"))
    for audio, language in [(b"toolong", "en"), (b"x", "invalid/language")]:
        r = await api.post("/api/transcriptions", files={"file": ("x.webm", audio, "audio/webm")}, data={"language": language})
        assert r.status_code == 400


@pytest.mark.parametrize("payload", [{}, {"text": None}, {"text": [KEY]}])
async def test_bad_transcription_result_is_not_exposed(api, monkeypatch, payload):
    await setup(api, "input")
    api.cfg.settings.features["speech_input"] = True
    upstream(monkeypatch, lambda r: httpx.Response(200, json=payload))
    r = await api.post("/api/transcriptions", files={"file": ("x.webm", b"x", "audio/webm")})
    assert r.status_code == 502 and KEY not in r.text


async def test_network_failure_does_not_expose_request_or_secret(api, monkeypatch):
    await setup(api)
    def fail(r):
        raise httpx.ConnectError(KEY, request=r)
    upstream(monkeypatch, fail)
    r = await api.get("/api/speech/output/voices")
    assert r.status_code == 502 and KEY not in r.text
