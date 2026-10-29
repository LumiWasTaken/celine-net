---
name: settings
description: The settings {{username}} sees in Settings: features, models and roles,
  tools, the context budget, the forms. Read them with settings__read; change them with
  settings__propose, each change once {{username}} approves it.
words: settings, setting, configure, config
---
`settings__read()` lists the targets, each with its label and version. A target is one
thing Settings saves at once:

- `features`: each feature on or off.
- `general`: the general settings and each skill's numbers (Advanced).
- `roles`: each role's model list, first choice first, with the models that fit it.
- `providers.<id>`: one provider: its type, name, address, options and models, and its
  key. `providers.<new id>` is a new provider.
- `tools`: each skill and tool, on or off.
- `services`: the addresses of the shellbox, the browser and web search.
- `email`, `vault`: their accounts.
- `context`: what you carry in one request: `notes` (a share of the window), `tools`
  (tokens), `idle_turns`, `memory_floor`, `memory_sections`, `memory_tokens`.
- `forms.<id>`: a plugin's form.

`settings__read(target)` gives its fields: key, label, type, the allowed values
(choices, min, max), the default, and the value now. A secret shows only its state, set
or not set; its value is never shown to you.

`settings__propose(target, changes, why)` changes fields of one target. `changes` is
`{key: new value}`; null puts a field back to its default. A secret is
`{"file": "~/path"}`, a file in your shellbox that holds it on one line; its value goes
from that file into Settings, and the file stays as it is. Null removes a secret.
Example: `settings__propose(target="general", changes={"turn_wall_clock_seconds": 3600},
why="Long research turns stop at the 30 minute limit.")`.

The change is checked at once, as Settings checks a save: a value Settings refuses comes
back with its sentence, and nothing is asked. Then {{username}} sees each field before
and after, and your why, with Approve and Deny, and the call waits for the answer.
Approved, it is saved as Settings saves it, and the result names each change. Denied or
no answer in time: nothing changes, and the result says so. When the target changed
between your proposal and the approval, nothing is saved and the result says it changed
since you proposed it.
