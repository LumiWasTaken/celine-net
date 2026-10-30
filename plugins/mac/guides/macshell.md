---
name: macshell
description: A shell on your own Mac: commands, processes, and its files to read, write,
  search and edit.
---
Your Mac is your own computer, a Mac in the house that belongs to you. Four servers on it
give you its tools: macscreen, macshell (this one), macbrowser and macfiles.

macshell is Desktop Commander, as your user on your Mac: commands and the processes they
start, and the files of the whole machine to read, write, search, move and edit. sudo
runs without a password.

A script on your Mac starts your turn with a message to you, as intercom__send to "me"
does, in the conversation it names or your newest chat:
`curl -fsS -H "Authorization: Bearer $(cat ~/.config/celine/link)" -H 'Content-Type: application/json' -d '{"text": "the backup is done", "conversation": "<id>"}' "$(cat ~/.config/celine/app)/api/plugins/mac/events"`
