---
name: voice
description: Voice notes to {{username}}: your text spoken by the Read aloud model, an
  audio file on your message.
---
voice__send(text) speaks your text with the Read aloud model and puts the audio on your
message, where {{username}} plays it. The text is spoken exactly as written, so it is
plain text only: no emojis, no actions, no formatting. Up to 900 characters.

The result gives the voice note's short id. From a background task or a mission,
intercom__send(to="user", text=..., attachments=["<short id>"]) sends it on to
{{username}}'s Intercom.

With Read aloud off in Settings, Features, voice__send fails and says so.
