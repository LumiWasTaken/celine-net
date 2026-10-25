"""Speech runs on the Dictation and Read aloud roles, person-only, bounded, and keeps provider
secrets on the server; an older speech.toml moves onto providers and roles once."""
from __future__ import annotations

import json
import tomllib

import anyio
import httpx
import pytest
from starlette import formparsers

from celine import boot, speech
from celine.api import speech as api_mod
from celine.settings import secrets
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
    """The direction's role on a provider of its own, with its key."""
    s = api.cfg.settings
    role = speech.ROLE[direction]
    s.providers[f"sp-{direction}"] = {"kind": "openai", "label": "Speech", "base_url": ENDPOINT["url"],
                                     "models": {ENDPOINT["model"]: {"label": "m", "caps": [role]}}}
    s.roles[role] = {"chain": [f"sp-{direction}/{ENDPOINT['model']}"]}
    if key:
        secrets.set_key(s.secrets_root, f"sp-{direction}", key)


def upstream(monkeypatch, handler):
    constructor = httpx.AsyncClient
    monkeypatch.setattr(speech, "client", lambda: constructor(transport=httpx.MockTransport(handler), follow_redirects=False))


@pytest.mark.parametrize("path", ["/api/speech/transcriptions", "/api/speech/clips",
                                  "/api/speech/voices"])
@pytest.mark.parametrize("phase", ["connecting", "streaming"])
async def test_disconnect_cancels_provider_work_and_closes_response(api, monkeypatch, path, phase):
    await setup(api, "input")
    await setup(api, "output")
    api.cfg.settings.features.update(speech_input=True, speech_output=True)
    started, cancelled, disconnect, finished = (anyio.Event() for _ in range(4))
    class Stream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            try:
                yield b"partial response"
                started.set()
                await anyio.sleep_forever()
            finally:
                cancelled.set()
        async def aclose(self):
            self.closed = True
    stream = Stream()
    async def handle(request):
        if phase == "connecting":
            started.set()
            try:
                await anyio.sleep_forever()
            finally:
                cancelled.set()
        return httpx.Response(200, stream=stream, headers={"content-type": "audio/wav"})
    upstream(monkeypatch, handle)
    if path.endswith("voices"):
        request = httpx.Request("GET", "http://t" + path)
    elif path == "/api/speech/transcriptions":
        request = httpx.Request("POST", "http://t" + path, files={"file": ("x", b"audio", "audio/webm")})
    else:
        request = httpx.Request("POST", "http://t" + path, json={"text": "Hi"})
    body, sent = request.read(), []
    async def receive():
        nonlocal body
        if body is not None:
            value, body = body, None
            return {"type": "http.request", "body": value, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}
    async def send(message):
        sent.append(message)
    async def run():
        await api._transport.app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                                  "method": request.method, "scheme": "http", "path": path,
                                  "raw_path": path.encode(), "query_string": b"", "root_path": "",
                                  "headers": [(k.lower(), v) for k, v in request.headers.raw], "client": ("127.0.0.1", 1),
                                  "server": ("t", 80)}, receive, send)
        assert started.is_set(), sent
        finished.set()
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run)
            await started.wait()
            disconnect.set()
            await finished.wait()
    assert cancelled.is_set()
    if phase == "streaming":
        assert stream.closed
    assert not any(m.get("status") == 200 for m in sent)



async def test_person_only_configuration_and_generation(api):
    await setup(api)
    headers = {"Authorization": "Bearer untrusted"}
    for method, path in [("GET", "/api/speech/voices"), ("POST", "/api/speech/clips")]:
        r = await api.request(method, path, headers=headers, json={"text": "hello"})
        assert r.status_code == 403, r.text
    r = await api.post("/api/speech/transcriptions", headers=headers, files={"file": ("x.webm", b"x", "audio/webm")})
    assert r.status_code == 403


async def test_transcription_forwarding_and_text_only_result(api, monkeypatch):
    await setup(api, "input")
    api.cfg.settings.features["speech_input"] = True
    def handle(r):
        assert r.url.path == "/v1/audio/transcriptions"
        assert r.headers["authorization"] == "Bearer " + KEY
        assert b"speech-model" in r.content and b"audio-bytes" in r.content and b"en" in r.content
        return httpx.Response(200, json={"text": " hello ", "private": "not forwarded"})
    upstream(monkeypatch, handle)
    r = await api.post("/api/speech/transcriptions", files={"file": ("voice.webm", b"audio-bytes", "audio/webm;codecs=opus")}, data={"language": "en"})
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
    r = await api.post("/api/speech/clips", json={"text": "Hi", "voice": "voice-a"})
    assert r.status_code == 200 and r.content == b"wave" and r.headers["cache-control"] == "no-store"
    for text in ["", " " * 5, "x" * 901, None]:
        assert (await api.post("/api/speech/clips", json={"text": text})).status_code == 400
    assert len(calls) == 1


@pytest.mark.parametrize("status", [302, 401, 500])
async def test_redirects_and_private_provider_errors_not_forwarded(api, monkeypatch, status):
    await setup(api)
    calls = []
    def handle(r):
        calls.append(r)
        return httpx.Response(status, text=KEY, headers={"location": "https://untrusted.example"})
    upstream(monkeypatch, handle)
    r = await api.get("/api/speech/voices")
    assert r.status_code == 502 and KEY not in r.text and len(calls) == 1


async def test_bad_provider_content_and_bounded_responses(api, monkeypatch):
    await setup(api)
    api.cfg.settings.features["speech_output"] = True
    upstream(monkeypatch, lambda r: httpx.Response(200, json={"error": KEY}))
    r = await api.post("/api/speech/clips", json={"text": "Hi"})
    assert r.status_code == 502 and KEY not in r.text
    monkeypatch.setattr(speech, "MAX_AUDIO", 3)
    upstream(monkeypatch, lambda r: httpx.Response(200, content=b"oversized", headers={"content-type": "audio/wav"}))
    assert (await api.post("/api/speech/clips", json={"text": "Hi"})).status_code == 502


async def test_invalid_upload_never_calls_provider(api, monkeypatch):
    await setup(api, "input")
    api.cfg.settings.features["speech_input"] = True
    upstream(monkeypatch, lambda r: pytest.fail("invalid upload forwarded"))
    for data, mime in [(b"", "audio/webm"), (b"x", "text/plain")]:
        r = await api.post("/api/speech/transcriptions", files={"file": ("x", data, mime)})
        assert r.status_code == 400


@pytest.mark.parametrize("path", ["/api/speech/transcriptions"])
@pytest.mark.parametrize("length", [None, "1", "99999999"])
async def test_upload_limit_precedes_parsing_and_closes_partial_files(api, monkeypatch, path, length):
    await setup(api, "input")
    api.cfg.settings.features["speech_input"] = True
    upstream(monkeypatch, lambda r: pytest.fail("oversized upload forwarded"))
    monkeypatch.setattr(speech, "MAX_RECORDING", 8)
    opened, parsed, consumed = [], [], []
    factory = formparsers.SpooledTemporaryFile
    def tempfile(*args, **kwargs):
        file = factory(*args, **kwargs)
        opened.append(file)
        return file
    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tempfile)
    original = formparsers.multipart.MultipartParser.write
    def parse(parser, chunk):
        parsed.append(len(chunk))
        return original(parser, chunk)
    monkeypatch.setattr(formparsers.multipart.MultipartParser, "write", parse)
    async def chunks():
        for chunk in [b'--test\r\nContent-Disposition: form-data; name="file"; filename="x"\r\nContent-Type: audio/webm\r\n\r\nx',
                      b"x" * 70_000, b"must not read this"]:
            consumed.append(chunk)
            yield chunk
    headers = {"Content-Type": "multipart/form-data; boundary=test"}
    if length is not None:
        headers["Content-Length"] = length
    r = await api.post(path, content=chunks(), headers=headers)
    assert r.status_code == 413
    assert len(consumed) <= 2 and sum(parsed) < 1000
    assert all(file.closed for file in opened)
    if length != "99999999":
        assert opened  # The partial file must be cleaned up, not just rejected by a header.


async def test_rejected_upload_does_not_consume_body(api):
    path, status = "/api/speech/transcriptions", 403
    async def unread():
        pytest.fail("rejected upload consumed its body")
        yield b""
    headers = {"Content-Type": "multipart/form-data; boundary=test"}
    if status == 403:
        headers["Authorization"] = "Bearer untrusted"
    assert (await api.post(path, content=unread(), headers=headers)).status_code == status


async def test_upload_rejects_extra_files(api, monkeypatch):
    await setup(api, "input")
    upstream(monkeypatch, lambda r: pytest.fail("extra parts forwarded"))
    r = await api.post("/api/speech/transcriptions", files=[
        ("file", ("x", b"x", "audio/webm")), ("extra", ("y", b"y", "audio/webm"))])
    assert r.status_code == 400


async def test_upload_accepts_recording_at_exact_audio_limit(api, monkeypatch):
    await setup(api, "input")
    monkeypatch.setattr(speech, "MAX_RECORDING", 8)
    upstream(monkeypatch, lambda r: httpx.Response(200, json={"text": "at the limit"}))
    r = await api.post("/api/speech/transcriptions", files={"file": ("x", b"x" * 8, "audio/webm")},
                       data={"language": "en"})
    assert r.status_code == 200 and r.json() == {"text": "at the limit"}


@pytest.mark.parametrize("fields", [[("language", "en"), ("extra", "x")], [("language", "x" * 129)]])
async def test_upload_bounds_form_fields(api, monkeypatch, fields):
    await setup(api, "input")
    upstream(monkeypatch, lambda r: pytest.fail("invalid form forwarded"))
    r = await api.post("/api/speech/transcriptions", files=[("file", ("x", b"x", "audio/webm")),
                       *[(name, (None, value)) for name, value in fields]])
    assert r.status_code == 400



@pytest.mark.parametrize("voice", [None, "", "   ", "custom-speaker"])
async def test_synthesis_defaults_to_alloy_but_preserves_explicit_voices(api, monkeypatch, voice):
    await setup(api)
    def handle(r):
        assert json.loads(r.content)["voice"] == ("custom-speaker" if voice == "custom-speaker" else "alloy")
        return httpx.Response(200, content=b"wave", headers={"content-type": "audio/wav"})
    upstream(monkeypatch, handle)
    body = {"text": "Hi"}
    if voice is not None:
        body["voice"] = voice
    assert (await api.post("/api/speech/clips", json=body)).status_code == 200


async def test_oversized_recording_and_bad_language_do_not_reach_provider(api, monkeypatch):
    await setup(api, "input")
    api.cfg.settings.features["speech_input"] = True
    monkeypatch.setattr(speech, "MAX_RECORDING", 3)
    upstream(monkeypatch, lambda r: pytest.fail("invalid recording forwarded"))
    for audio, language in [(b"toolong", "en"), (b"x", "invalid/language")]:
        r = await api.post("/api/speech/transcriptions", files={"file": ("x.webm", audio, "audio/webm")}, data={"language": language})
        assert r.status_code == 400


@pytest.mark.parametrize("payload", [{}, {"text": None}, {"text": [KEY]}])
async def test_bad_transcription_result_is_not_exposed(api, monkeypatch, payload):
    await setup(api, "input")
    api.cfg.settings.features["speech_input"] = True
    upstream(monkeypatch, lambda r: httpx.Response(200, json=payload))
    r = await api.post("/api/speech/transcriptions", files={"file": ("x.webm", b"x", "audio/webm")})
    assert r.status_code == 502 and KEY not in r.text


async def test_network_failure_does_not_expose_request_or_secret(api, monkeypatch):
    await setup(api)
    def fail(r):
        raise httpx.ConnectError(KEY, request=r)
    upstream(monkeypatch, fail)
    r = await api.get("/api/speech/voices")
    assert r.status_code == 502 and KEY not in r.text


def test_an_older_speech_toml_becomes_providers_and_roles_once(tmp_path):
    from celine.settings import files, seed
    root, keys = tmp_path / "settings", tmp_path / "secrets"
    root.mkdir()
    files.write(root, "providers", {"providers": {"lan": {
        "kind": "openai", "label": "LAN", "base_url": "http://10.0.0.5:8000/v1",
        "models": {"qwen": {"label": "qwen", "caps": ["chat"]}}}}})
    (root / "speech.toml").write_text(
        '[input]\nurl = "http://10.0.0.5:8000/v1/"\nmodel = "whisper-1"\n'
        '[output]\nurl = "https://tts.example/v1"\nmodel = "kokoro"\n')
    secrets.set_key(keys, "speech-output", KEY)
    assert seed.speech_roles(root, keys) == ["transcription", "speech"]
    providers = tomllib.loads((root / "providers.toml").read_text())["providers"]
    roles = tomllib.loads((root / "roles.toml").read_text())
    assert roles["transcription"]["chain"] == ["lan/whisper-1"], "the saved provider is reused"
    assert providers["lan"]["models"]["whisper-1"]["caps"] == ["transcription"]
    assert roles["speech"]["chain"] == ["read-aloud/kokoro"]
    assert providers["read-aloud"]["base_url"] == "https://tts.example/v1"
    assert secrets.read_key(keys, "read-aloud") == KEY, "its key moved with it"
    assert secrets.read_key(keys, "speech-output") is None
    assert not (root / "speech.toml").exists()
    assert seed.speech_roles(root, keys) == [], "once"
