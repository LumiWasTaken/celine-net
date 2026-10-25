# Speech

Dictation and Read aloud are independent, optional features. Both are off on new and
existing installs. They are offered during onboarding and in Settings, Features. No
speech engine, model download, container or extension framework is included.

## Provider settings

For each feature, enter the provider's OpenAI-compatible base address (including `/v1`),
model ID and optional API key. The app must be able to reach that address; it is not a
browser address. Input and output may use the same gateway or different providers.
Use Discover models to save the address and key and fetch the provider's model list.
Pick a returned model, or enter an ID manually if the provider does not expose an
inventory. Run Record test for Dictation or Test speech for Read aloud before turning
on the feature. Tests work while the feature is off and never submit a chat message.
Changes take effect without a restart.

The app calls these fixed provider routes:

| Feature | Route | Request |
|---|---|---|
| Dictation | `POST /audio/transcriptions` | Multipart `file`, `model`, `response_format=json`, optional `language` |
| Read aloud | `POST /audio/speech` | JSON `model`, `input`, `voice`, `response_format=wav` |
| Voice discovery | `GET /voices` | Optional list as `{ "voices": ["name", { "id": "name" }] }` |
| Model discovery | `GET /models?service=stt` or `?service=tts` | OpenAI envelope `{ "object": "list", "data": [{ "id": "model-id" }] }` |

Addresses may already end in `/v1` or another provider API prefix; the app appends the
route, not an extra `/v1`. A proxy must authorize the transcription and speech scopes,
not just model discovery. No personal server addresses or keys are shipped.

Keys are write-only, stored in `var/secrets/speech-input.key` and
`speech-output.key`, not in browser preferences or the settings TOML. Empty key input
preserves an existing key; Clear saved key removes it. Changing an address requires
replacing or explicitly clearing its saved key. Redirects are rejected and upstream
error bodies are never returned. These routes are for the logged-in person, not agent
or connector bearer tokens. With authentication disabled, the app's existing
person-only policy applies; protect the instance accordingly.

The feature status says whether an address and model are configured. It does not
claim that recording, the model, provider authorization or playback was tested.
The optional `service` query is a gateway filter, not an OpenAI standard. Generic
providers can ignore it and return their usual inventory. Speech capability metadata,
when provided, filters out the other speech direction; untyped entries remain
selectable rather than guessing capabilities from model names.
Record test displays the actual transcript and releases the microphone on finish or
Stop test. Test speech makes a real synthesis request and displays audio controls for
the returned sample. Use those controls if the browser blocks autoplay. Discovery
alone never counts as a successful speech test. Neither test enables the feature.
Opening Speech options discovers voices only. Providers without `/voices` still
work: enter a voice name manually. Voice discovery does not test transcription.
An empty voice uses `alloy`; select or enter another voice if the provider requires it.

## Dictation

Use the microphone beside Send, then Finish recording. The transcript is added to
the existing editable draft. Speech options offers a language code and Send after
transcription, off by default. When enabled, it submits the whole composer, including
the existing draft and ready attachments, through the normal send path. Empty
transcripts, errors and cancellation never submit.

Recording stops after two minutes or at 12 MB. Cancel, navigation, switching chats,
disconnecting, disabling the feature and unmounting release microphone tracks and
invalidate pending work, including a late permission response. Microphone capture
needs HTTPS or localhost and browser permission. A Bluetooth headset may switch its
audio profile when its microphone opens.

Both transcription routes bound incoming bytes before multipart parsing to 12 MB plus
64 KiB of form overhead, even without a trustworthy Content-Length. They accept one
file and an optional language field; the recording itself is still limited to 12 MB.
Leaving a speech request cancels its provider work, including explicit tests and
model/voice discovery. Navigating away also stops a test microphone recording, even
if microphone permission is still pending.

## Playback

Read aloud on a completed assistant reply plays only its answer text. Reasoning and
tool output are not included. Fenced code, URLs, Markdown/HTML and speech control tags
are removed; prose is split at sentences or newlines into at most 900-character
requests. One producer generates ahead, one request at a time, while clips play in
order. At most one clip is queued beyond the playing clip, including while paused or
waiting for an autoplay gesture. Responses are limited to 20 MB; provider requests
time out after three minutes.
Chat uses a page-mounted native audio player, as the setup test does. Its controls
appear during playback for manual play, pause and volume adjustments. Browser media
errors include their diagnostic code instead of hiding the cause behind a generic
playback error.

Speech options holds the voice and optional automatic reading of new successful
replies. Automatic reading is off by default and does not replay history, aborted or
failed turns. Voice, language, automatic reading and automatic sending are device
preferences, not server credentials. A saved voice remains selected while discovery
loads and across refreshes.

Stop playback immediately pauses audio, aborts the active browser request and clears
queued clips. Navigation and feature changes also stop playback. Already-running
provider GPU work may ignore cancellation. If browser autoplay restrictions block a
clip, Play audio resumes that same clip with a user gesture, without generating it
again. Stop also cancels a clip waiting for that gesture.

## Verification

`uv run pytest -q` includes speech configuration, authorization, forwarding, bounded
responses, secret handling and redirect tests on fake providers. `cd web && npm run
build` includes deterministic prose splitting, continuous lookahead and cancellation
checks before TypeScript and Vite. Real microphone permission, audible playback,
provider scopes and model compatibility require a browser/provider smoke test.
