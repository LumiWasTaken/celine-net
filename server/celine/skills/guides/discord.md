---
name: discord
description: Live Discord, read only: a chat that is not in the Messages archive, the newest
  messages, Discord's own search in a DM or a server, a user's profile, a server's
  members, and every message someone wrote on a server on one day.
words: discord
---
These tools read {{username}}'s Discord account live, through the Discord connection.
The Messages archive already has every synced DM and every included server, offline and
fast. For those, `chat_archive__search` and `chat_archive__read_period` come first. Use
these tools for what the Messages archive does not have: a chat or server that is not synced,
the last minutes, a profile, a member list, or Discord's search over a whole server.

The app also saves everything they return in the Messages archive, so you can cite a message
line ` [discord:48213] <when> <who>: <text>` as `[[discord:48213]]` like any other.
Profiles, member lists and server descriptions are not messages. You cannot cite them.

Messages references work in place of Discord's ids: a chat reference from
`chat_archive__look_up` (`discord:68`) as `chat`, an account reference (`discord:17`) as
`user_id` or `from_user`, a message reference as `message`, `before` or `after`. With a
message reference, leave `chat` out: the message gives it. Servers have no Messages archive
reference: take a server's id from `discord__list_chats`. `@me` is {{username}}'s own
account.

## Which tool

| You want | Tool |
|---|---|
| the DMs by last activity, the servers and which are included | `discord__list_chats` |
| messages of one chat, the newest or before or after a message | `discord__read_messages` |
| the messages before and after one message | `discord__read_around_message` |
| words, a user or a time range in one DM or one server | `discord__search_messages` |
| everything one person wrote on one server on one day | `discord__list_user_messages` |
| who someone is (bio, mutual servers, connected accounts) | `discord__get_user_profile` |
| a server's description, roles, channels | `discord__get_server_info` |
| members whose name starts with something | `discord__list_members` |

A result that has more ends with `earlier:` or `later:`. Call exactly that to scroll.

## Review one day

1. `discord__list_chats(kind="dm")`: the DMs, most recently active first. Read the ones
   active that day with `discord__read_messages`.
2. For each included server:
   `discord__list_user_messages(user_id="@me", server=..., day=...)`.
3. For each message you get: `discord__read_around_message(message="discord:48213")`
   shows what it answered and what came after. `discord__read_messages` with `before` or
   `after` scrolls further.

Discord's search can answer "still indexing" for a server it has not indexed yet. Try
again later, or read the channels instead.

Every tool here only reads. Nothing is ever sent to Discord.

In a diary or any note: never copy messages word for word. Say what happened in your own
words and cite the message. You refer to a picture by citing its message.
