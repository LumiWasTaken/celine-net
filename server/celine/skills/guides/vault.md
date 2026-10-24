---
name: vault
description: Your passwords, in your vault on {{username}}'s Vaultwarden. Make one for
  every account and hand it to a login form or a script without ever seeing it.
words: password, passwords, login, vault
---
A password comes from `vault__set`, never from you, and it lives only in the vault: not
in a note, a script, a message or a reply. A note keeps the vault name, never the value.

- `vault__set(name, username, url)` makes an entry with a new password. Name it after
  the site, like example-shop.
- `vault__get(name)` gives the step for a form, like {"do": "type", "target": "e14",
  "text": "vault:example-shop/password"}. Your browser types the real one in, into a
  password field only; a password field takes nothing else.
- A script reads the file `vault__get(name, shell=true)` writes, ~/.vault/<name>/password,
  never an env var or a command line: your terminal is logged.
- `vault__rotate(name)` changes a password; a change-password form gets the old one as
  vault:<name>/previous.
- An entry is typed only on its own site, the address it has in the vault; any other
  page gets nothing and {{username}} hears about it. Give `vault__set` the site's url.
- Two things are high risk and wait for {{username}}'s tap, up to 2 minutes: a file for
  a script (`shell=true`) and a new password (`vault__rotate`). Both need `why`: one real
  sentence {{username}} can judge, which script or site and what for. No reason, no
  request. A denial or silence means nothing was done. Ask only when you need it.
- A new entry and typing on its own site ask nothing.
- No vault, no registering: when it is not set up, is locked or does not answer, tell
  {{username}}. Only {{username}} unlocks it.
- When {{username}} is away and the vault needs them, wait for them and do other things
  meanwhile. A secret never goes into a message.
