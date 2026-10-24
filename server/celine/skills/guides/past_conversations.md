---
name: past_conversations
description: Your own earlier conversations with {{username}} in this app, your sub-agents'
  and your background runs' included. Load it when they ask what you and they talked about
  before, or to review a day of your work. "We" in a question about their WhatsApp, Discord
  or other chats means the Messages archive, not this.
---
Your past conversations with {{username}} in this app, your bridged WhatsApp
chat with them included. Their own WhatsApp and Discord chats with other people are not
here: those are in the Messages archive (chat_archive). Your sub-agents' conversations and
your background runs are here too.

- `past_conversations__search(query)`: word search over what was written, on the version
  each conversation is on now. The current conversation is left out. FTS5 syntax works:
  quotes for a phrase, OR, and `prefix*`. Each match names its conversation id.
- `past_conversations__list_day(date)`: the index of one day: every conversation with messages
  that day, its message numbers that day, its turns, its tool calls by name and the ones
  that failed, its first and last line. A run's try says its try and how it ended; a try
  that failed after writing something says "(a draft, not a source)". Any other conversation whose last turn
  failed, was interrupted or aborted says so ("last turn aborted").
- `past_conversations__read(conversation, start, end, tools)`: one conversation in order.
  Each tool call is one line with its id and how it ended (`tools="full"` adds its
  arguments and result). A long one comes in slices: without `start` from the end back,
  with `start` on from there. Its `earlier:` and `later:` lines read on.
- `past_conversations__read_tool_call(id)`: one tool call raw, its arguments and its whole
  result.

A whole day: the day's index first, then read only what matters, and a raw call only
when its arguments or result matter. On a long day, give groups of conversations to
sub-agents with a brief and wait for their reports.

Example: `past_conversations__search(query="pizza OR pasta")`, then
`past_conversations__read(conversation="3f9c2a1b")`.
