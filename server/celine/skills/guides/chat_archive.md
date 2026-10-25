---
name: chat_archive
description: {{username}}'s Messages archive (WhatsApp, Discord, every imported chat). Use it for
  any question about what was said, by whom, when, or how often. Start with
  chat_archive__search, or chat_archive__count for numbers.
words: whatsapp, discord, archive
---
The Messages archive holds {{username}}'s own chats. You do not remember it: every question
about it starts with a tool call, and the answer comes only from what the tools showed you.
Never answer from memory, and never write that you searched when no tool ran. Use these
tools only. Never use the terminal for it, and never look for database files.

What you and {{username}} said to each other in this app, your own runs included, is
not here: that is past_conversations.

A name you do not know (a nickname, an alias, "Kit") is most likely someone in the
archive: `chat_archive__look_up(name=...)` first. It knows the other names {{username}}
gave a person, and lists the chats each person writes in, most messages first. Ask
{{username}} only when the look-up finds nobody.

A name is weak in `about`: "Kit is a person" describes no situation, so it matches noise.
A name goes in `words`. For what Kit said, pass their `person` reference in `written_by`.
For what others said about Kit, pass `words=["Kit"]`.

## Which tool

| The question | Tool |
|---|---|
| what was said about X, when did we first or last, every time | `chat_archive__search` |
| how many, how often, when most, who most, at night, did I ever say X | `chat_archive__count` |
| a person or chat is named ("with Noor", "in the car group") | `chat_archive__look_up` first, then pass its references |
| summarise a chat over a period, what did we decide | `chat_archive__read_period(chat=…, when=…)` |
| the whole chat session around a line you found | `chat_archive__read_session(ref=…)` |
| what led up to a line, what came after it | `chat_archive__scroll(ref=…)` |
| a reference you saved or saw before, the message itself | `chat_archive__get_messages(refs=[…])` |
| two accounts are the same person (only after {{username}} says so) | `chat_archive__mark_same_person` |

## Two words for the units of chat text

- A **chat session** is the messages of one chat between two pauses longer than 90
  minutes. `chat_archive__read_session` returns one.
- A **match window** is one search result: the messages of one chat from the first to the
  last matching passage, plus up to 2 messages before and after. Matches less than 30
  minutes apart join one match window.

## about and words: semantic topic search first

`chat_archive__search` has two kinds of query. They do different things:

- `about`: one to three full sentences that describe the messages you want, the way you
  would retell them: who, what, the situation. The semantic topic search compares them by
  embedding, and a reranker sorts the results by how well they fit these sentences. No
  word has to match. Always pass `about`.
  Good: "Noor describes what she enjoys about hiking", "someone complains that the
  game server lags", "{{username}} and Noor plan a trip".
  Bad: "tight", "i want", "favorite" (keywords are not a description).
- `words`: exact words or phrases someone typed. Use it only when such a word exists: the
  name of a thing, a slang word, a phrase. Several words form one exact phrase. The result
  counts each entry exactly ("exact word matches in scope"). A count of 0 means nobody
  typed it.

## How chat_archive__search works

1. The app cuts each chat into chat sessions, and each chat session into passages of a
   few messages. Every passage has an embedding vector: a point that stands for what the
   passage is about.
2. The app turns your `about` sentences into vectors too and takes the nearest passages.
   That is the semantic topic search. It finds a passage that says the same thing in
   other words, another language or slang. It is weak on one exact rare word, a name or a
   number.
3. Your `words` go to the word search index: exact words (the last letters may differ:
   `celine` also finds `celines`) and exact phrases. Word search is strong for names,
   slang and a typed phrase. It does not find what was said in other words.
4. The app merges both lists. A reranker scores the best fifty candidates, each with its
   neighbouring messages: a model reads your `about` sentence next to the passage and
   judges the fit. The order you see is that judgement. `>` marks the lines that matched.
5. A match window is a lead, not proof. Read it before you rely on it. "nothing clearly
   about it" means the reranker found no good fit. Then change the sentence, not only the
   words.

When to change the query: few or off-topic results from `about`: describe it differently
(the situation, the feeling, what someone would say), or in the other language. A word
count of 0: nobody typed it, so use `about`. Thousands of word matches: the word is too
common. Narrow with `about`, `when` or `scope`. A good match window: read it, then search
with the words and people you saw there.

## Open questions: go deep, in waves

For an open question (what someone likes, wants, thinks or did over time, "search
deeply", "find everything about"), one or two searches are never enough. Work in waves
and make many calls. There is no limit on searches or steps.

1. **Broad search.** Make three to six searches, each with other `about` sentences:
   different angles, other languages if the chats use them, direct and indirect ("she says what she likes",
   "she complains about what she does not like", "she asks for something"). Use
   `mode="all"` when you want many match windows.
2. **Read.** Read the best four to eight match windows in full with
   `chat_archive__read_session(ref=...)`. A line from a search is not the whole chat
   session. The answer is often in the messages around it. Where it gets interesting,
   scroll with `chat_archive__scroll` (next section).
3. **Follow the leads.** Make new searches from what you read: the words people actually
   used, the people and chats involved (`chat_archive__look_up`, then references), the
   periods when it came up (`when`). `chat_archive__count` with those words shows how
   often and when.
4. **Check the other side.** Search for the opposite, exceptions and changes over time.
   The answer must not rest on one match window.
5. **Stop** when two new angles bring nothing new (the progress line says so). Then answer
   with everything you found, with citations.

A closed question (a date, a count, "did I ever say X") needs one or two calls.

## Scroll around a match

A match is one spot in a chat. What led up to it and what came next is often the answer.
Scroll like {{username}} would on the phone:

- `chat_archive__scroll(ref="whatsapp:48213", direction="before")`: up to 20 older
  messages.
- `direction="after"`: up to 20 newer messages. `direction="both"`: each side.
- You only get messages that are not in your view yet. The messages you have are one line
  ("12 messages you already have"). To scroll further, call it again with the same
  reference, or with the reference the result names under "older" or "newer".
- When the app shortened older results to save room, their messages count as not in
  view, and `chat_archive__scroll` returns them again.
- The result ends with the range of the chat you now have. It says when you reached the
  start of the chat or the newest message.

When to scroll: a match starts in the middle of something ("haha yes exactly", "as I
said"), someone answers a question you cannot see, a plan is made and you want to know if
it happened, or it looks interesting. Stop when the topic changes.

Example: a search shows `[whatsapp:48213]` where Noor says "ok fine, you win". What did
she give in on? Call `chat_archive__scroll(ref="whatsapp:48213", direction="before")`,
and again if the start is still further up.

## Saving references

A reference like `whatsapp:48213` never changes. It works in any later conversation:
`chat_archive__get_messages(refs=["whatsapp:48213", "whatsapp:50110"])` returns the
messages with their chat, who wrote them and when.

Collect the good ones in your notebook, in a note like `refs.md` or `links/<topic>.md`
(your choice), one line each: the reference, the date, who, and what it is in a few of
your own words. For example:

    - [[whatsapp:48213]] 2023-05-02, Noor: gives in about the Portugal trip
    - [[discord:9921]] 2024-01-10, Sam: first time the server lagged, blames the update

Write only the reference and your words, not a copy of the message. When you use a saved
reference later, get it with `chat_archive__get_messages` first, then cite it.

## scope: where you look, on purpose

Every search and count needs `scope`:
- `["all"]`: every chat on every source. Use it when nothing narrows the question.
- source ids, such as `["discord"]`: "on Discord", "only WhatsApp".
- chat references from `chat_archive__look_up`, such as `["whatsapp:213", "discord:68"]`:
  a named chat or group, or "my chats with X". X usually has one direct message per
  source: pass every one.

Always pass people and chats as references, never as names: two people can share a name.
`chat_archive__look_up(name="Noor")` gives `person whatsapp:17 ...` and
`chat whatsapp:213 ...` lines. A person reference goes in `written_by`, a chat reference
in `scope` or `chat_archive__read_period(chat=...)`. `chat_archive__read_session` takes
a message reference, the `[whatsapp:48213]` of a message line. After a look-up that found
one person, or a search with `written_by`, reads mark that person's lines with `>`.

`written_by` filters who wrote a message. It does not choose chats. A reference in
`written_by` covers every account marked as the same person. "What did Noor say about X"
is `written_by=["whatsapp:17"]` with `scope=["all"]`. "What did we talk about in my chat
with Noor" is `scope=[<her direct message references>]`.

`when` takes dates only: `2021`, `2021-03`, `2021-03..2021-05`, `last summer`, `letzten
Sommer`. A time of day goes into `hours` of `chat_archive__count` (`hours="22-6"` is
22:00 to 05:59, "at night"). `chat_archive__search` cannot filter by the hour.

Every result starts with a `scope:` line (and `when:` or `person:` lines). Check them. If
they are not what you meant, search again with the right arguments.

## Examples

- "When did I first talk about Celine?"
  `chat_archive__search(about=["{{username}} talks about setting up Celine"], words=["celine"], scope=["all"], order="first")`
- "What does Noor think of the weekend plans?" (open: go deep)
  `chat_archive__look_up(name="Noor")`, then several
  `chat_archive__search(about=["Noor suggests a plan for the weekend"], scope=["whatsapp:213"], mode="all")`
  with other angles, `chat_archive__read_session(ref=...)` of the best match windows,
  then new searches from what you read.
- "Who is Kit?" (four calls)
  `chat_archive__look_up(name="Kit")` gives `person discord:265 ...` and the chats they
  write in. Then one `chat_archive__search(about=["Kit talks about their work and what
  they do"], written_by=["discord:265"], scope=["discord:129"])` over their busiest chat,
  one `chat_archive__read_session(ref=...)` of the best match window, then answer.
- "How often did we talk about sushi, per year?"
  `chat_archive__count(by="year", scope=["all"], words=["sushi"])`
- "At what time of day do Noor and I write most?"
  `chat_archive__count(by="hour", scope=["whatsapp:213"])`
- "Who writes to me most in the evening?"
  `chat_archive__count(by="person", scope=["all"], hours="18-23")`
- "What went on in my group chats last month?"
  `chat_archive__count(by="chat", scope=["whatsapp"], when="last month")` lists the chats
  with their kind (group, dm) and references. Then read the busiest groups with
  `chat_archive__read_period`.
- "Summarise last week with Noor."
  `chat_archive__look_up(name="Noor")`, then
  `chat_archive__read_period(chat="whatsapp:213", when="last week")`. The summary cites a
  few of the messages it is based on: the plan, the news, the photos.
- "Did I ever mention Portugal on Discord?"
  `chat_archive__count(by="total", scope=["discord"], words=["portugal", "portugiesisch"])`:
  a zero answers it in one call. A number tells you to search next.

## Progress and results: narrow down, do not repeat

There is no limit on searches or steps. Depth means exploring what you found, not
searching everything again with other words. Every Messages archive result ends with a
progress line (searches so far, messages seen). A repeated call returns "same as call N"
and does not count as a call. When the last two calls showed nothing new, the progress
line says you have enough to answer.

Each search uses what the last ones showed you:
- A chat keeps coming up: search inside it, `scope=["whatsapp:213"]`.
- A person is involved: `chat_archive__look_up`, then `written_by` or their direct
  messages as `scope`.
- A match is close but not it: `chat_archive__scroll(ref=...)`.
- You know roughly when: `when` on a narrower search, or
  `chat_archive__read_period(chat=..., when=...)`.
- The words might never have been typed: `chat_archive__count(by="chat", words=[...])`
  shows where they occur, or that they do not.

After three searches in a row that find nothing new, the result gets a note with the
chats and matches you already have. This counts searches inside one chat too: other
words, other years or the next chat do not help when nothing is there. After six, the app
does not run a search of the whole archive. After eight, it runs no search at all. A read or a
scroll that shows new messages starts the count again. Searches that keep finding new
messages are never stopped. If nothing is left to try, answer with what you found. Say
plainly what you could not find.

## Citing, not quoting

Every message line has a reference like `whatsapp:48213`. Cite what you use as
`[[whatsapp:48213]]`, right after the sentence it supports, and say who wrote it. Only
cite references a tool showed you in this conversation. Get a saved reference with
`chat_archive__get_messages` first. A summary of a period or a chat session cites too: a
few of the messages it is based on, one for each point that matters.

Tell what was said in your own words. The citation is the proof: {{username}} can click it
and read the message. So write "Noor said she was done with the trip [[whatsapp:48213]]",
not a block of quoted lines.

Quote only when the exact words are the point: a promise, a line that is funny only word
for word, a word someone made up. Then quote the exact words of that message, in
quotation marks, and read its chat session (`chat_archive__read_session(ref=…)`) first.
The app checks the answer against the messages.

## Your answer

While you search: when you change direction (narrowing to a chat or a person, scrolling
around a match, a new angle), write one short plain line before the calls, like "nothing
in the group chat, trying your DM with Noor" or "found the trip, scrolling up to see who
said yes". Write it only when the direction changes. Never write filler ("let me look",
"got it", "I have a picture now").

In the answer, say once, in plain words, where you looked: "I searched all your chats",
"only your DM with Noor on WhatsApp", "Discord only, 2025". Then answer. Do not retell the
searches: the tool rows show them. Numbers come from `chat_archive__count`, never from
counting results.
