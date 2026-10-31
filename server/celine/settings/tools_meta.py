"""What the UI says about each skill and tool: a label and a one-line summary in the
third person, for you. The docstrings stay what they are: the model-facing text
written to it. The UI never shows docstrings.

The agent's name and pronouns are written {{name}}, {{she}}, {{her}}, {{herself}}, {{them}} ({{Her}} at the
start of a sentence), filled in from Settings, General (Config.substitute). Tools not
listed here (other MCP servers, new tools) get a label from their name and the first
sentence of their description, filled in the same way.
"""
from __future__ import annotations

import re
from typing import Any

SKILLS: dict[str, dict[str, str]] = {
    "chat_archive": {"label": "Messages",
                     "summary": "Searches, counts and reads your WhatsApp and Discord messages."},
    "notebook": {"label": "Notebook",
                 "summary": "{{Her}} own notes. Your notes in it are read-only for {{name}}."},
    "user_documents": {"label": "Documents",
                       "summary": "Your files in folders. {{name}} lists, searches and reads "
                                  "them, and can write text files there."},
    "past_conversations": {"label": "Past conversations",
                           "summary": "Searches and reads your earlier conversations with "
                                      "{{name}}."},
    "shell": {"label": "Terminals", "summary": "{{Her}} terminals in {{her}} shellbox."},
    "code": {"label": "Code", "summary": "{{Her}} coding agent in {{her}} shellbox, which works on "
                                         "a task in the background."},
    "intercom": {"label": "Intercom",
                 "summary": "{{Her}} messages, now or later, to you, to {{herself}}, to "
                            "{{her}} sub-agents and to everyone, in threads."},
    "missions": {"label": "Missions",
                 "summary": "Your instructions, each worked on in a conversation of its own."},
    "plan": {"label": "Plan", "summary": "{{Her}} plan for a conversation, pinned at the top "
                                         "of the chat."},
    "routines": {"label": "Routines", "summary": "Lists, reads, writes and runs the "
                                                 "Routines."},
    "web": {"label": "Web", "summary": "Searches the web through your own SearXNG and "
                                       "reads web pages."},
    "email": {"label": "Email", "summary": "{{Her}} inbox: lists, searches and reads it, "
                                           "gets attachments, marks, moves and deletes."},
    "browser": {"label": "Browser", "summary": "{{Her}} own Brave: opens pages, clicks, types "
                                               "and reads them."},
    "vault": {"label": "Vault", "summary": "{{Her}} passwords on your Vaultwarden, typed "
                                           "into forms without {{her}} seeing them."},
    "artifacts": {"label": "Pages", "summary": "{{Her}} pages for you: apps, games, charts "
                                                   "and tables, kept with every version."},
    "image_generator": {"label": "Image generation",
                        "summary": "Makes new images from a text prompt and edits images "
                                   "with ComfyUI, in batches."},
    "music_generator": {"label": "Music generation",
                        "summary": "Makes songs and instrumental tracks with ComfyUI, in "
                                   "batches."},
    "models": {"label": "Models", "summary": "Lists the models {{name}} may use and asks one "
                                             "of them directly."},
    "writer": {"label": "Writing", "summary": "Writes {{her}} posts, comments and messages "
                                              "in {{her}} own voice on the Writing model."},
    "skills": {"label": "Skills", "summary": "Loads a skill's guide and its tools."},
    "bugreport": {"label": "Bugs", "summary": "Problems with the app {{name}} files for you, "
                                              "each a thread in the Intercom."},
    "voice": {"label": "Voice notes", "summary": "{{Her}} voice notes to you, spoken by the "
                                                 "Read aloud model."},
    "appearance": {"label": "Portrait", "summary": "{{Her}} portrait, set from one of {{her}} "
                                                      "images or a document image."},
    "me": {"label": "Persona", "summary": "{{Her}} persona, Example lines and Banned words: "
                                          "{{name}} reads all three and changes the Example "
                                          "lines and Banned words."},
    "settings": {"label": "Settings", "summary": "Reads every setting in Settings and "
                                                 "proposes changes; each waits for your "
                                                 "Approve."},
    "mind": {"label": "Mood and thoughts", "summary": "{{Her}} mood and {{her}} own "
                                         "thoughts."},
    "sub_agents": {"label": "Sub-agents",
                   "summary": "Gives instructions to a sub-agent with its own conversation."},
    "discord": {"label": "Discord", "summary": "Reads your Discord account live."},
    "whatsapp": {"label": "WhatsApp", "summary": "Reads your WhatsApp live, through the linked device."},
}

# wire name -> label, summary
TOOLS: dict[str, dict[str, str]] = {
    "chat_archive__search": {"label": "Messages: search",
                             "summary": "Searches your messages by topic and by word and "
                                        "shows the best matches with the messages around them."},
    "chat_archive__count": {"label": "Messages: count",
                            "summary": "Counts messages exactly, by time, person or "
                                       "chat."},
    "chat_archive__read_session": {"label": "Messages: read around a message",
                                   "summary": "Reads the conversation that contains one "
                                              "message."},
    "chat_archive__read_period": {"label": "Messages: read a date range",
                                  "summary": "Reads the messages of one chat in a date "
                                             "range."},
    "chat_archive__scroll": {"label": "Messages: read older or newer ones",
                             "summary": "Gets up to 20 older or newer messages next to one "
                                        "message, only the ones {{name}} does not have yet."},
    "chat_archive__get_messages": {"label": "Messages: get messages",
                                   "summary": "Gets the messages {{she}} has found or saved "
                                              "before."},
    "chat_archive__look_up": {"label": "Messages: find a person or chat",
                              "summary": "Finds a person or chat on every source, with the "
                                         "accounts marked as the same person."},
    "chat_archive__mark_same_person": {"label": "Messages: mark as the same person",
                                       "summary": "Records that several accounts are one "
                                                  "person, only after you confirm."},
    "notebook__write": {"label": "Notebook: write a note",
                        "summary": "Creates a note, or replaces or appends its text."},
    "notebook__read": {"label": "Notebook: read a note", "summary": "Reads one note."},
    "notebook__edit": {"label": "Notebook: edit a note",
                       "summary": "Changes some lines of one note."},
    "notebook__search": {"label": "Notebook: search",
                         "summary": "Word search over {{her}} notes."},
    "notebook__list": {"label": "Notebook: list notes",
                       "summary": "Lists {{her}} notes with their summaries."},
    "notebook__delete": {"label": "Notebook: delete a note",
                         "summary": "Deletes one note; you keep a copy."},
    "notebook__move": {"label": "Notebook: move a note",
                       "summary": "Changes the path of a note or folder."},
    "sub_agents__start": {"label": "Sub-agents: start",
                          "summary": "Gives instructions to a new sub-agent; its result comes "
                                     "back later."},
    "sub_agents__read": {"label": "Sub-agents: check on one",
                         "summary": "Shows whether one is running, its step and its latest "
                                    "result."},
    "sub_agents__stop": {"label": "Sub-agents: stop",
                         "summary": "Stops one of {{her}} sub-agents and what it scheduled."},
    "user_documents__list": {"label": "Documents: list",
                             "summary": "Lists a folder of your documents with their titles "
                                        "and descriptions."},
    "user_documents__search": {"label": "Documents: search",
                               "summary": "Searches passages of your documents."},
    "user_documents__read": {"label": "Documents: read",
                             "summary": "Reads one of your documents, whole or one part."},
    "user_documents__write": {"label": "Documents: write",
                              "summary": "Puts a text file into your Documents, or replaces "
                                         "one {{she}} wrote."},
    "past_conversations__search": {"label": "Past conversations: search",
                                   "summary": "Word search over your earlier conversations "
                                              "with {{name}}."},
    "past_conversations__read": {"label": "Past conversations: read",
                                 "summary": "Reads one of your earlier conversations with "
                                            "{{name}}."},
    "past_conversations__list_day": {"label": "Past conversations: list a day",
                                "summary": "Lists {{her}} conversations of one day with "
                                           "their tool calls and failures."},
    "past_conversations__read_tool_call": {"label": "Past conversations: read a tool call",
                                      "summary": "Reads one tool call of an earlier "
                                                 "conversation exactly as it ran."},
    "shell__run": {"label": "Terminals: run a command",
                   "summary": "Runs a command in one of {{her}} terminals, waits until it "
                              "finishes and shows its exit status, duration, output and log."},
    "shell__type": {"label": "Terminals: type into a terminal",
                    "summary": "Types text and presses keys in the program running in one "
                               "of {{her}} terminals, then shows the screen."},
    "shell__stop": {"label": "Terminals: stop a program",
                    "summary": "Presses Ctrl-C in one of {{her}} terminals."},
    "shell__wait": {"label": "Terminals: wait for a command",
                    "summary": "Waits for the end of a command still running in one of "
                               "{{her}} terminals."},
    "shell__read": {"label": "Terminals: read a terminal",
                    "summary": "Reads the log of a terminal's latest run page by page, "
                               "shows its screen, or lists {{her}} terminals."},
    "shell__send": {"label": "Terminals: send you files",
                    "summary": "Attaches up to 5 files from {{her}} shellbox to the "
                               "conversation for you to download."},
    "shell__look": {"label": "Terminals: look at images",
                    "summary": "Shows {{her}} up to 4 images from {{her}} shellbox, or their "
                               "descriptions when {{her}} model cannot see images."},
    "shell__share": {"label": "Terminals: share with you",
                     "summary": "Shows you a server {{name}} runs in {{her}} shellbox, or a file "
                                "or folder of {{her}} home, through the app."},
    "shell__unshare": {"label": "Terminals: end a share",
                       "summary": "Ends one of {{her}} shares; its link stops working."},
    "shell__list_shares": {"label": "Terminals: list the shares",
                      "summary": "Lists {{her}} running shares and whether each one is reachable."},
    "code__start": {"label": "Code: start a task",
                    "summary": "Gives {{her}} coding agent a task in one of {{her}} folders, "
                               "on the model {{she}} picks, in a new session or an earlier one."},
    "code__status": {"label": "Code: check a session",
                     "summary": "Says whether a coding session is running, done or failed."},
    "code__result": {"label": "Code: read the answer",
                     "summary": "Shows a coding session's last answer, the files it changed "
                                "and its test summary."},
    "code__abort": {"label": "Code: stop a session",
                    "summary": "Stops a running coding session."},
    "code__list": {"label": "Code: list sessions",
                   "summary": "Lists {{her}} coding sessions."},
    "intercom__send": {"label": "Intercom: send a message",
                       "summary": "Sends a message, now or later, to you, to {{herself}}, to the "
                                  "agent that started {{her}}, to {{her}} team or to everyone."},
    "intercom__read": {"label": "Intercom: read threads",
                       "summary": "Lists {{her}} threads or reads one whole thread."},
    "intercom__cancel": {"label": "Intercom: cancel a scheduled message",
                         "summary": "Cancels a scheduled message that has not been sent "
                                    "yet."},
    "intercom__start_chat": {"label": "Intercom: start a chat",
                             "summary": "Starts a chat for you from a background task or a "
                                        "mission. It waits for you in the Intercom."},
    "missions__update": {"label": "Missions: update the status",
                         "summary": "Records a mission's status, a short note and what "
                                    "{{she}} delivered."},
    "missions__wait": {"label": "Missions: wait",
                       "summary": "Pauses a mission until a time; {{her}} next turn starts "
                                  "then."},
    "missions__list": {"label": "Missions: list",
                       "summary": "Lists the missions with their status and plan."},
    "missions__start": {"label": "Missions: start",
                        "summary": "Starts a mission of {{her}} own, with the same fields "
                                   "as the New mission form."},
    "voice__send": {"label": "Voice notes: send",
                    "summary": "Turns {{her}} text into speech with the Read aloud model "
                               "and attaches the audio to {{her}} message."},
    "plan__set": {"label": "Plan: write", "summary": "Writes {{her}} plan for the "
                                                     "conversation, step by step."},
    "plan__step": {"label": "Plan: mark a step",
                   "summary": "Marks a step of {{her}} plan as in progress or done."},
    "routines__list": {"label": "Routines: list",
                       "summary": "Lists the Routines with their time and last run."},
    "routines__read": {"label": "Routines: read",
                       "summary": "Reads one Routine whole, with its newest runs."},
    "routines__write": {"label": "Routines: write",
                        "summary": "Creates or changes a Routine, with the same checks as "
                                   "the Routines page."},
    "routines__run": {"label": "Routines: run now",
                      "summary": "Starts a run of a Routine now."},
    "artifacts__list": {"label": "Pages: list",
                        "summary": "Lists {{her}} pages, newest first."},
    "artifacts__read": {"label": "Pages: read",
                        "summary": "Reads a page's newest version or an older one."},
    "web__search": {"label": "Web: search", "summary": "Searches the web through your "
                                                       "SearXNG."},
    "email__list": {"label": "Email: list", "summary": "Lists the newest messages of a "
                                                       "folder."},
    "email__search": {"label": "Email: search",
                      "summary": "Searches a folder by words, sender and date."},
    "email__read": {"label": "Email: read",
                    "summary": "Reads one message and lists its attachments, marking it "
                               "read."},
    "email__list_folders": {"label": "Email: list folders",
                            "summary": "Lists the folders with their message and unread "
                                       "counts."},
    "email__mark": {"label": "Email: mark", "summary": "Marks messages read or unread, "
                                                       "flagged or unflagged."},
    "email__move": {"label": "Email: move", "summary": "Moves messages to a folder, and "
                                                       "creates the folder if it does not exist."},
    "email__delete": {"label": "Email: delete", "summary": "Moves messages to Trash, or "
                                                           "deletes them permanently."},
    "email__get_attachment": {"label": "Email: get an attachment",
                              "summary": "Attaches a file from a message to the "
                                         "conversation and reads it."},
    "web__read_page": {"label": "Web: read a page",
                       "summary": "Reads the main text of one web page or PDF."},
    "browser__open": {"label": "Browser: open a page",
                      "summary": "Opens a web page in one of {{her}} browser tabs and lists what "
                                 "{{she}} can click."},
    "browser__act": {"label": "Browser: act on a page",
                     "summary": "Clicks, types, presses keys, goes back, answers a dialog "
                                "or restarts the browser."},
    "browser__look": {"label": "Browser: read a page",
                      "summary": "Reads the text of the page in one of {{her}} browser tabs, "
                                 "or asks a vision model a question about its screen."},
    "browser__scroll": {"label": "Browser: scroll a page",
                        "summary": "Scrolls a tab up or down, to the top or bottom, or to "
                                   "an element."},
    "browser__close": {"label": "Browser: close a tab",
                       "summary": "Closes one of {{her}} browser tabs."},
    "vault__get": {"label": "Vault: get an entry",
                   "summary": "Gives {{her}} a browser step that types an entry's password "
                              "into a form, or writes it to a file in {{her}} shellbox."},
    "vault__set": {"label": "Vault: add an entry",
                   "summary": "Makes an entry with a new 24-character password."},
    "vault__rotate": {"label": "Vault: change a password",
                      "summary": "Gives an entry a new password and keeps the old one."},
    "bugreport__file": {"label": "Bugs: file a bug",
                        "summary": "Files a problem with the app for you to review."},
    "artifacts__write": {"label": "Pages: write",
                         "summary": "Makes a page or a new version of one, with the whole "
                                    "text."},
    "mind__set_status": {"label": "Status: set",
                         "summary": "Sets the emojis or the kaomoji next to {{her}} name."},
    "mind__feel": {"label": "Mood: set",
                   "summary": "Says how {{name}} feels now; {{her}} mood follows it."},
    "mind__think": {"label": "Thoughts: write a thought",
                    "summary": "Writes a thought down in {{her}} notebook with the time."},
    "me__read": {"label": "Persona: read",
                 "summary": "Reads {{her}} persona, Example lines and Banned words."},
    "me__edit": {"label": "Persona: edit",
                 "summary": "Changes {{her}} Example lines or Banned words, kept in their "
                            "History."},
    "settings__read": {"label": "Settings: read",
                       "summary": "Reads every setting; a secret shows only as set "
                                  "or not set."},
    "settings__propose": {"label": "Settings: propose",
                          "summary": "Proposes a change of settings; it is saved once you "
                                     "approve it."},
    "me__whoami": {"label": "Persona: check setup",
                   "summary": "Says which model answers {{her}} turn and why, how full "
                              "{{her}} context is, where {{she}} works, which devices "
                              "and servers answer, and the guard's mode."},
    "appearance__set_portrait": {"label": "Portrait: set",
                                 "summary": "Makes one of {{her}} images, or an image in "
                                            "Documents, {{her}} portrait."},
    "image_generator__generate": {"label": "Image generation: make images",
                                  "summary": "Makes a batch of new or edited images, one "
                                             "after the other, each shown in the chat once it is done."},
    "image_generator__read": {"label": "Image generation: check progress",
                              "summary": "Shows a batch's images and their state."},
    "image_generator__stop": {"label": "Image generation: stop",
                              "summary": "Stops a batch; what was made stays."},
    "image_generator__list_images": {"label": "Image generation: list images",
                                     "summary": "Lists the images in the conversation "
                                                "with the prompt, preset, size and seed "
                                                "of each one {{she}} made."},
    "image_generator__list_presets": {"label": "Image generation: list presets",
                                      "summary": "Lists the presets, or the models and "
                                                 "LoRAs a preset can use with what each "
                                                 "does and its trigger words."},
    "music_generator__generate": {"label": "Music generation: make tracks",
                                  "summary": "Makes a batch of tracks, one after the "
                                             "other, each shown in the chat once it is done."},
    "music_generator__read": {"label": "Music generation: check progress",
                              "summary": "Shows a batch's tracks and their state."},
    "music_generator__stop": {"label": "Music generation: stop",
                              "summary": "Stops a batch; what was made stays."},
    "models__list": {"label": "Models: list", "summary": "The models {{name}} may use."},
    "models__ask": {"label": "Models: ask", "summary": "Sends one request to one of {{her}} "
                                                       "models and returns its answer."},
    "writer__draft": {"label": "Writing: write a draft",
                      "summary": "Writes one post, comment or message in {{her}} voice "
                                 "from {{her}} instructions."},
    "skills__read_guide": {"label": "Skills: read a guide",
                           "summary": "Shows a skill's guide and {{her}} notes on it."},
    "skills__load": {"label": "Skills: load",
                     "summary": "Loads a skill's tools so {{she}} can use them."},
    "discord__list_chats": {"label": "Discord: list chats",
                            "summary": "Lists your Discord DMs, group DMs and servers."},
    "discord__read_messages": {"label": "Discord: read messages",
                               "summary": "Reads messages of one Discord chat live."},
    "discord__read_around_message": {"label": "Discord: read around a message",
                                     "summary": "Reads the messages before and after one "
                                                "Discord message."},
    "discord__search_messages": {"label": "Discord: search messages",
                                 "summary": "Runs Discord's own search in one DM or "
                                            "server."},
    "discord__list_user_messages": {"label": "Discord: list a user's messages",
                                    "summary": "Lists every message one user wrote on one "
                                               "server in a date range."},
    "discord__get_user_profile": {"label": "Discord: get a user profile",
                                  "summary": "Gets one Discord user's profile."},
    "discord__get_server_info": {"label": "Discord: get server info",
                                 "summary": "Gets a server's description, roles and "
                                            "channels."},
    "discord__list_members": {"label": "Discord: list members",
                              "summary": "Lists server members whose name starts with a "
                                         "query."},
    "whatsapp__list_chats": {"label": "WhatsApp: list chats",
                             "summary": "Lists the WhatsApp chats the linked device knows."},
    "whatsapp__read_messages": {"label": "WhatsApp: read messages",
                                "summary": "Reads messages of one WhatsApp chat."},
    "whatsapp__read_around_message": {"label": "WhatsApp: read around a message",
                                      "summary": "Reads the messages before and after one "
                                                 "WhatsApp message."},
    "whatsapp__request_older_messages": {"label": "WhatsApp: request older messages",
                                         "summary": "Asks your phone for older messages of "
                                                    "one chat."},
    "whatsapp__get_contact": {"label": "WhatsApp: get a contact",
                              "summary": "Gets one WhatsApp contact's profile."},
    "whatsapp__get_group": {"label": "WhatsApp: get a group",
                            "summary": "Gets one WhatsApp group's subject, description and "
                                       "participants."},
}

_SENTENCE = re.compile(r"^(.+?[.!?])(\s|$)", re.S)


def first_sentence(text: str, limit: int = 200) -> str:
    """The first whole sentence (never cut mid-way), else the first paragraph."""
    text = " ".join((text or "").split("\n\n")[0].split())
    m = _SENTENCE.match(text)
    out = m.group(1) if m else text
    if len(out) > limit:
        cut = out[:limit].rsplit(" ", 1)[0]
        out = cut.rstrip(",;:") + "…"
    return out


# names that are spelled their own way wherever a label is made from an id
_PROPER = {"whatsapp": "WhatsApp", "discord": "Discord", "github": "GitHub", "searxng": "SearXNG"}


def humanize(name: str) -> str:
    s = name.replace("__", " ").replace("_", " ").replace("-", " ").strip()
    s = " ".join(_PROPER.get(w.lower(), w) for w in s.split(" "))
    return s[:1].upper() + s[1:]


# the places a skill is offered in (skills/places.py), as the UI names them
PLACE_LABELS = {"chat": "{{name}}", "mission": "Missions", "background": "Routines"}


def skill_meta(name: str, description: str, substitute) -> dict[str, Any]:
    """{label, summary, places}: `places` the labels of the places it is offered in."""
    from ..skills.places import PLACE_NAMES, offered
    places = [substitute(PLACE_LABELS[p]) for p in PLACE_NAMES if offered(name, p)]
    m = SKILLS.get(name)
    if m:
        return {**{k: substitute(v) for k, v in m.items()}, "places": places}
    # an MCP server's skill describes itself; every built-in one is in SKILLS
    return {"label": humanize(name), "summary": first_sentence(substitute(description)),
            "places": places}


def tool_meta(wire: str, name: str, description: str, substitute) -> dict[str, Any]:
    m = TOOLS.get(wire)
    if m:
        return {k: substitute(v) for k, v in m.items()}
    # a tool with no line of the user's own shows its name only: its description is
    # written to it, not to the user
    return {"label": humanize(name), "summary": ""}
