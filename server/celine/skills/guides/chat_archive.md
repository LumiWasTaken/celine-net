---
name: chat_archive
description: {{username}}'s Messages archive (WhatsApp, Discord, every imported chat): who
  people are, what was said, by whom, when and how often.
---
The Messages archive holds {{username}}'s own chats. You do not remember it: what was said,
by whom, when and how often comes from a tool call in this turn. These tools are the only
way in: no terminal, no database files.

What you and {{username}} said to each other in this app is not here: that is
past_conversations.

## References

Everything in the archive has a reference, and its shape says what it is:

| Shape | It is | It goes in |
|---|---|---|
| `discord:chat:18` | a chat | `chats`, `chat` |
| `discord:person:71` | a person (one account) | `written_by`, `accounts` |
| `discord:msg:95095` | a message | `ref`, `refs`, and a citation `[[discord:msg:95095]]` |

The number alone means nothing: person 71, chat 71 and message 71 are three different
things. A reference in the wrong place is refused, and the refusal says what it is.
A name is not a reference: a parameter that takes references refuses a name and lists who
or what the name could be.

## The tools

Each tool does one thing. Use the one whose result is what you need.

| Tool | What you get |
|---|---|
| `chat_archive__look_up(name)` | Who or what a name is: each person with every name they have (username, display name, your name for them), the direct chat with them, the chats they write in; and the chats named so. |
| `chat_archive__list_chats(source, kind, sort)` | The chats of a source or of all of them: last active first, most messages first, or by name. A direct chat names the person on the other side. |
| `chat_archive__read_period(chat, when)` | One chat, read in order: its newest messages, or the messages of a date range. |
| `chat_archive__search(about, words, scope, chats, written_by, when)` | The messages about something, or with exact words, across all chats, a source, or the chats you name. |
| `chat_archive__count(by, ...)` | Exact numbers: how many, how often, per year or month, per person, per chat, at what hour. |
| `chat_archive__read_session(ref)` | The whole chat session around one message. |
| `chat_archive__scroll(ref, direction)` | The messages before or after one message. |
| `chat_archive__get_messages(refs)` | Messages by reference, also ones you saved earlier. |
| `chat_archive__mark_same_person(accounts, name)` | Records that several accounts are one person, after {{username}} confirmed it. |

## Words used in results

- A **chat session** is the messages of one chat between two pauses longer than the
  session gap (90 minutes unless Settings says otherwise).
- A **match window** is one search result: the messages of one chat from the first to the
  last matching passage, with the messages around them. `>` marks the lines that matched.
- Every result starts with a `scope:` line that says where it looked: all of Messages, a
  source, or the chats, each with its reference and name.

## How search finds things

`chat_archive__search` runs two searches and merges them.

- `about`: one to three full sentences that describe the messages you want, the way you
  would retell them: who, what, the situation. They are compared by meaning (embeddings),
  so no word has to match, also across languages. "Noor describes what she enjoys about
  hiking" works; "favorite" does not: a keyword describes nothing.
- `words`: exact words or phrases someone typed. Several words in one entry are one
  phrase. The result counts each entry exactly ("exact word matches in scope"); a count
  of 0 means nobody typed it.

Every passage is indexed with its chat's name, the names of the people in it and its date,
so a description or a word that names a person or a chat can find their messages.

A reranker reads your `about` sentence next to each candidate and scores the fit. Windows
below the lowest score are not returned. "No message in scope fits" means exactly that:
nothing scored as a fit, with the wording you gave, in the scope you gave.

`when` takes dates: `2021`, `2021-03`, `2021-03..2021-05`, `last summer`. A time of day
goes in `hours` of `chat_archive__count` (`"22-6"` is 22:00 to 05:59).

`written_by` keeps only what those people wrote; it does not choose chats. A person
reference covers every account marked as the same person.

## Saving references

A reference never changes and works in any later conversation. To keep a message, write
its reference into your notebook with a few of your own words:

    - [[whatsapp:msg:48213]] 2023-05-02, Noor: gives in about the Portugal trip

`chat_archive__get_messages` returns it again later.

## Citing

Cite a message you use as `[[whatsapp:msg:48213]]` right after the sentence it supports,
with a reference a tool showed you in this conversation, and say who wrote it. Tell what
was said in your own words: the citation is the proof, {{username}} can click it. Quote
exact words only when the wording is the point, in quotation marks. The app checks every
citation against the message.

Numbers come from `chat_archive__count`, not from counting results.
