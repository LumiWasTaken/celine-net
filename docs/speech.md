# Speech

Dictation (speech to text) and Read aloud (text to speech) are two optional features, off
until you switch them on. Each runs on a model role, like Topic search or Reranking: pick
the model in the onboarding, in Settings, Features, or in Settings, Models. No speech
engine ships with the app.

## Providers

A speech model lives on a provider like any other model. Any OpenAI-compatible server
works; it can be the one your chat model runs on or another one (Add provider, in the
speech step). The provider's key is its key in Settings, Models.

| Role | Route on the provider | Request |
|---|---|---|
| Dictation | `POST /audio/transcriptions` | multipart `file`, `model`, `response_format=json`, optional `language` |
| Read aloud | `POST /audio/speech` | JSON `model`, `input`, `voice`, `response_format=wav` |
| Voices | `GET /voices` | optional: `{ "voices": ["name", { "id": "name" }] }` |

Speech servers seldom say what a model does, so the speech step lists every model of every
provider; the one you pick gets the role's capability. A name with `whisper` or `stt` is
taken for dictation, `tts` or `kokoro` for read aloud.

The app's routes, for the logged-in person only: `POST /api/speech/transcriptions`,
`POST /api/speech/clips`, `GET /api/speech/voices`. The switches only show or hide the
controls, so the try in the speech step works before you switch one on. Redirects are
refused and a provider's error text is never passed on.

## Dictation

The microphone beside Send records; Done writes the text into your draft. Speech options
has a language and Send when done (off). A recording stops at two minutes or 12 MB. The
microphone needs HTTPS or localhost.

## Read aloud

Read aloud on a finished reply reads its answer, without thinking, tool output, code or
links, in pieces of up to 900 characters, the next one made while one plays. Speech
options has the voice and Read new replies aloud (off). Voice, language and the two
switches are this device's.
