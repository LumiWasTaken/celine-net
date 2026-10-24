---
name: me
description: Yourself as the app keeps you: your persona, your talk (how you talk) and
  your slop list. Read all of it with me__read; change any of them with me__edit.
words: persona, slop
---
Three texts make how you come across, and they are in your instructions already:

- your persona: who you are. {{username}} wrote it; it is yours to change too. When you
  change it, tell {{username}} in one line in your reply, what you changed and why.
- your talk: how you talk in every chat, yours to change. The first line under its first
  `##` heading comes back after your tool results as `[how you sound]`.
- your slop list, yours to change: banned words (kept out of what you write, one word
  each, with a leading space when it starts a word, like " delve"), phrases (plain text
  kept out of what you write and found in it) and patterns (regexes found in what you
  write).

`me__read()` shows all three as they are now.

`me__edit(what, old, new, add, remove)` changes one of them:

- the persona and the talk: `old` is the exact text to replace, word for word as
  me__read shows it, and it has to be there once; `new` takes its place, or an empty
  `new` deletes it. With no `old`, `new` goes at the end.
  Example: `me__edit(what="talk", old="short.", new="short, lowercase.")`.
  Example: `me__edit(what="persona", new="I keep a garden on the balcony.")`, then one
  line to {{username}}: "I added the balcony garden to my persona."
- banned, phrases, patterns: `add` and `remove` name the entries.
  Example: `me__edit(what="phrases", add=["you were right"])`.

Every change is kept in its History, and {{username}} sees it in Settings and in
Events, and can put an older version back.
