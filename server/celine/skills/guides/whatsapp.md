---
name: whatsapp
description: Live WhatsApp through {{username}}'s linked device: the chats by last
  activity, a chat's newest messages, older messages requested from the phone, a
  contact's or a group's details.
words: whatsapp
---
These tools read what {{username}}'s WhatsApp linked device has, and can request older
messages from {{username}}'s own phone. The Messages archive has the same chats, offline and
fast: `chat_archive__search` and `chat_archive__read_period` come first. Use these tools
for the last minutes, for older messages the Messages archive does not have yet, and for a
contact's or a group's details.

You can cite a message line ` [whatsapp:48213] <when> <who>: <text>` as
`[[whatsapp:48213]]`. Contact and group details are not messages. You cannot cite them.

Messages references work in place of WhatsApp's ids: a chat reference from
`chat_archive__look_up` (`whatsapp:213`) as `chat`, a message reference as `message`,
`before` or `after`. Then leave `chat` out: the message gives it.
`whatsapp__get_contact` takes the phone number that `chat_archive__look_up` shows. A
group's JID comes from `whatsapp__list_chats`.

## Which tool

| You want | Tool |
|---|---|
| the chats by last activity | `whatsapp__list_chats` |
| messages of one chat, the newest or before or after a message | `whatsapp__read_messages` |
| the messages before and after one message | `whatsapp__read_around_message` |
| messages older than the oldest the device has (asks the phone, up to 2 minutes) | `whatsapp__request_older_messages` |
| a contact's name, about text, picture | `whatsapp__get_contact` |
| a group's subject, description, participants | `whatsapp__get_group` |

A result that has more ends with `earlier:` or `later:`. Call exactly that to scroll.
A request to the phone works only while the phone is online. Nothing is ever sent to a
contact. Every tool here only reads.
