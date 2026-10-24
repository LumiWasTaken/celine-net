# Speech

Dictation and Read aloud are independent, optional features. Both are off on new and
existing installs. They are offered during onboarding and in Settings, Features. No
speech engine, model download, container or extension framework is included.

## Provider settings

For each feature, enter the provider's OpenAI-compatible base address (including `/v1`),
model ID and optional API key. The app must be able to reach that address; it is not a
browser address. Input and output may use the same gateway or different providers.
Save the settings, then turn on the feature. Changes take effect without a restart.

The app calls these fixed provider routes:

| Feature | Route | Request |
|---|---|---|
| Dictation | `POST /audio/transcriptions` | Multipart `file`, `model`, `response_format=json`, optional `language` |
| Read aloud | `POST /audio/speech` | JSON `model`, `input`, `voice`, `response_format=wav` |
| Voice discovery | `GET /voices` | Optional list as `{ "voices": ["name", { "id": "name" }] }` |

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
Opening Speech options discovers voices only. Providers without `/voices` still
work: enter a voice name manually. Voice discovery does not test transcription.

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

## Playback

Read aloud on a completed assistant reply plays only its answer text. Reasoning and
tool output are not included. Fenced code, URLs, Markdown/HTML and speech control tags
are removed; prose is split at sentences or newlines into at most 900-character
requests. One producer generates ahead, one request at a time, while clips play in
order. Responses are limited to 20 MB; provider requests time out after three minutes.

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
