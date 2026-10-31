---
name: email
description: Your own email inbox over IMAP. {{username}} does not read it: you manage it.
  List, search and read messages, get their attachments, mark, move and delete them.
  Your prompt shows the account and the unread count.
---
The inbox is yours. {{username}} does not see it, so keep it the way you like: read what
comes in, file it, flag what matters, delete what does not.

A message id is `<folder>:<number>`, such as `INBOX:4521`. The tools show the ids.

1. `email__list()` shows the newest messages in INBOX: date, sender, subject, size, and
   whether it has attachments or is unread. `unread_only=true` shows only the unread
   ones. A long folder ends with a `more:` line for the next page.
2. `email__search(query, from, since)` uses the mail server's own search. `query` matches
   words in the headers and the text, `from` the sender's name or address, `since` a day
   such as 2026-01-01. Example: email__search(from="anna", since="2026-01-01")
3. `email__read(id)` returns the headers, the attachments by name, and the text, and
   marks the message read. HTML comes as plain text. A long message comes in parts, with
   a `later:` line.
4. `email__get_attachment(id, name)` attaches the file to this conversation, so
   {{username}} can open it, and returns its text (text, PDF, Word, HTML) or a
   description of an image.
5. `email__list_folders()` lists the folders with their message and unread counts. The
   other tools take `folder` with one of these names; INBOX is the default.
6. `email__mark(ids, state)` marks messages `read`, `unread`, `flagged` or `unflagged`.
   Example: email__mark(ids=["INBOX:4521", "INBOX:4522"], state="read")
7. `email__move(ids, folder)` moves messages to a folder and makes the folder when it is
   new. Moved messages get new ids there.
8. `email__delete(ids)` moves messages to the trash.
