---
name: intercom
description: Messages to {{username}}, to yourself, to your sub-agents or parent, and to
  everyone working, now or later, in threads like mail.
---
intercom__send(text, to) writes a message. `to` is one or more of, comma separated:
- "user": {{username}}.
- "me": yourself.
- "parent": in a sub-agent, the conversation that started it, for a question or a note
  while you work. Your result goes in your final answer: that is your report, and it
  goes up by itself.
- "team": your own sub-agents.
- "all": every conversation working right now (a turn or a job running) and every
  active mission, without you.
- "bastet": Bastet, the guard of the house.
- a sub-agent's or mission's name, or a conversation id.
A sub-agent writes to "parent", its own sub-agents and "me".

Bastet guards the house: when she stops a step, your result says so in her words, with
why. If you disagree, or want to talk, write to her; she answers in the thread.
She gives no passes and no unfreezes: only {{username}} does.

Every message from an agent or the app has a number (#412), and so does a reply of
{{username}}'s in a thread (re #412); their plain chat messages have none. reply_to=412
answers a message in its thread, with its subject; without `to` the answer goes to
whoever wrote #412. A thread reads like a chat: when your turn answers a message in a
thread (your instructions name it), what you send to its writer goes into that thread
by itself. new_thread=true starts a new thread. When {{username}} replies in a thread, your answer
goes into that thread for them.

intercom__read lists your threads by who they are with, newest activity first: one
line per counterpart ("with scout: 4 threads, 2 waiting"), then its 3 newest threads
and a count of the rest. {{username}}'s plain chat messages are no threads: they are
in the conversation. Job results are one line ("2 job results, newest #530"), listed
with kind="job". with="scout" (a name, "user" or a conversation id) lists every thread
with them, kind="report" (message, brief, report, job, notice, trigger) only messages
of that kind, unread=true only threads with messages waiting for you.
intercom__read(thread=409) shows one whole thread.

A message to "user" is in {{username}}'s Intercom section in the app, where they read and
answer it. wait=true holds your turn
on the message until {{username}} answers, up to 10 minutes: they see that you wait for
them, and their answer comes in as their message; it does not work with in or at.

attachments=["f5ef648c"] sends files along by their short ids: one on your own message
(a voice note, a file you sent, an image you made) or one {{username}} attached here.

in="30 min", at="02:00" or at="2026-11-02 09:00" makes a message land later. To "me" that is a reminder: at
its time it arrives here and starts your turn, or joins the turn you are in at its next
step. Use it for servers, slow things and later checks.

until="05:00" on a message to "all" makes it an announcement: everyone working gets it
now, and every turn that starts before 05:00 sees it in its instructions.

A message reaches a conversation with a turn running at its next step, and starts a
turn in one without. Some land without starting a turn: the result of a job that was
stopped, the report of a sub-agent that was stopped, a mission's status note, and
anything sent to a stopped mission. {{username}}'s reply to a message a stopped mission
sent resumes that mission. Your instructions list the announcements standing
now, what you scheduled (with its number), and your team (working: a turn, a job or a
wake of its own still ahead).
intercom__cancel(id) cancels a scheduled message that has not landed; it stays listed as
cancelled.

In a background task or a mission, intercom__start_chat(title, text) starts a chat for
{{username}}: the text is your first message there and waits for them on the intercom;
when they answer, you go on there with them. Check intercom__read first: when they have
it already, add to that thread. The result lists what they got from the same place in
the last 24 h (a bug report still open, a thread), with what it says and whether they
answered. The title is the topic in 2 to 4 plain words.
