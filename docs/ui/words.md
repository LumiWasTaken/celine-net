# Words

Every word on screen is in the table below, is plain content (a name, a message,
a title someone wrote), or is an icon with a tooltip from this table. The `words-banned`
block at the end of this file lists the words no UI string uses.

## Rules

1. One word per job, the same in every place. "Search" is always Search.
2. Only standard words: what ChatGPT, Claude, Apple or Google
   call the same thing. A new word goes into the table first, with who else uses
   it, before it goes into the code.
3. Sentence case; British spelling ("colour"); counts as "3 chats"; dates en-GB.
4. A help text (the words behind a "?") comes from the server: at most two sentences and
   220 characters, ending with "."; it says what the thing does, never repeats its label,
   never says "this setting" or "click", no dashes and no " · "; the agent is named by the
   server's placeholder ({{name}}), filled before it is sent, never by a pronoun
   ({{pronouns}} is only for prompts). A self-explaining label gets none.

## Approved words

"Same word in" names apps that use it for the same thing.

### Places

| Word | In Celine | Same word in |
|---|---|---|
| (its name) | your chats with the agent (the place takes the agent's name) | ChatGPT, Claude: the chats are the home |
| Missions | long tasks it works on | |
| Routines | its schedules and their runs | Google Home, Alexa "Routines" |
| Notebook | its own notes | ChatGPT "Memory" for the same job |
| Intercom | messages between it, missions and sub-agents; Everyone is the recipient that reaches every running agent | |
| Documents | your files | Google Docs, Apple Files |
| Messages | the WhatsApp and Discord archive. A chat is yours with the agent (Lock archived chats), or inside Messages a WhatsApp or Discord chat (Search chats, Choose chats): the place says which | Apple "Messages", Google "Messages" |
| Terminals | its terminals | macOS Terminal |
| Browser | its browser (never the engine's name); Current tab is the tab on its screen | Chrome "Current tab" |
| Events | everything it did and everything that ran, one list | GitHub "Events", Google Cloud "Events"; Discord "Audit Log" for the same job |
| Search | the archive search, and every search box | ChatGPT "Search chats", Claude "Search" |
| Settings | settings | everyone |
| Setup | the onboarding (/welcome), opened again from the top of the Settings list | macOS "Setup Assistant", Windows "Setup" |
| (its name) | the first Setup step, titled with the name the user typed: name, pronouns, colour, persona, example lines | character creators |

### Lists and items

| Word | In Celine | Same word in |
|---|---|---|
| New chat, New mission, New note, New folder, New document, New schedule, New terminal | the one primary New X in a head (New note is the Notebook's; Documents makes a New document) | ChatGPT, Claude "New chat"; Apple "New Note" |
| Search | the search field and its placeholder | everyone |
| Sort: Recent, Name, Created | sort order, in the Toolbar | Finder "Sort By", Google Drive |
| Group: Folder, Person, State, None | grouping, in the Toolbar | Finder "Group By" |
| State, Show | the Toolbar's state filter; Show where the choice is which items a list shows, not a state (Intercom: All, Guard) | Gmail "Show" |
| Folder, Person | the Toolbar's filters | |
| Date: Today, This week, This month, This year | the date filter of Search, in the Toolbar | Gmail, Google Drive "Modified" |
| All messages | every source's chats in one list, in Messages | Gmail "All Mail" for the same job |
| Select, Select all, Done | selecting many | Apple Photos, Mail |
| Archive, Archive older than | move out of the list, kept | ChatGPT "Archive", Gmail |
| Restore | back from the Archive or the Trash | Google Drive, Windows |
| Trash, Move to Trash, You can restore it from the Trash. | the bin and moving there; the body of every Move to Trash? confirm | macOS, Gmail, Google Drive |
| Delete permanently | only inside the Trash | Google Drive "Delete forever", Outlook "Permanently delete" |
| Delete | a message, a note version (menus); a stored secret or setting is removed (Remove) | everyone |
| Remove | take a stored thing out: a key, a token, a passkey, a device, the portrait, one recent search | Bitwarden, Chrome "Remove" |
| Open | open the item | everyone |
| Open Settings, Open Setup, Open chat, Open mission, Open terminal | a link that leaves names where it goes (Open Setup: the feature's own Setup step while it is on) | VS Code "Open Settings", "Open in Terminal" |
| Back to Settings | the way out of Setup opened again from Settings, never on the first run | Gmail "Back to Inbox" |
| Undo | take back a reversible action from its toast (Archive one item), or the composer's text a command replaced; never a delete or a move to Trash, those ask first | Gmail "Undo", everyone |
| Unsaved, Unsaved changes | an edit not saved yet, in an editor with Save (a note, a document); Settings save themselves | Google Docs, VS Code "Unsaved changes" |
| Rename, Edit, Duplicate, Copy | | ChatGPT, Claude, Finder |
| Pin, Unpin | keep at the top | Slack, Discord, ChatGPT |
| Export, Download, Upload | | everyone |
| Favourites, Add to Favourites, Remove from Favourites | images and tracks kept as favourites (a Toolbar toggle, the heart) | Google Photos, Apple Photos "Favourites" |
| Unread | a notification or message you have not seen (a count) | Gmail, Slack |
| Direct messages | the Intercom Type filter for plain messages (beside Instructions, Results, Jobs, Notices, Triggers); not Messages, which is the archive place | Slack "Direct messages", Discord "Direct Messages" |
| Play, Pause | the mini player of a track | everyone |
| Copied | what Copy says once it has copied | ChatGPT, Claude "Copied" |
| Variants, Variant | the images made for one image block, pick one of four | Leonardo "Variations" |
| Creating image | an image on its way | ChatGPT "Creating image" |
| Notifications | the Settings page of the system notifications while the tab is hidden | everyone; Chrome, macOS "Notifications" settings |
| Show more | the end of a long list | Google |
| More | the "..." menu and its tooltip | Google "More" |
| Clear search | empty the search field | Gmail |
| Recent | recent items | ChatGPT, Claude "Recents" |
| Type | what kind of file it is (a filter, a field) | Google Drive "Type" filter, Windows Explorer "Type" |
| Book, Books | an EPUB document and its type | Apple Books |
| Versions, History, Previous version, Next version | earlier versions | Google Docs "Version history"; ChatGPT version arrows |
| Restore this version | make an older version the current one | Google Docs, Claude artifacts |
| Version 3, Download version 3, Version 3, 14:02 | a page's versions | Google Docs "Version" |
| Not found, Nothing at this link. | the page a link opens when what it names is not there (a run, a mission, a provider, a text) | everyone |
| No chats match, No notes match | a list its search or its filters empty: No (things) match | plain |
| No chats yet, No notes yet | an empty list that its head's New starts: No (things) yet, no second New in it | ChatGPT's empty history |
| Discard | drop the edits an editor has not saved (its Unsaved changes line) | Google Docs, Gmail "Discard" |
| Pause, Resume | a terminal's live output: hold it still, let it go on | YouTube, everyone |
| Run now | Routines: a schedule's run started by hand | GitHub Actions "Run workflow" |

### Messages and turns

| Word | In Celine | Same word in |
|---|---|---|
| Regenerate | a new reply or draft in place of this one | ChatGPT, Claude |
| Continue | a reply: write on from where it stopped | ChatGPT "Continue generating" |
| Retry | do again what failed or was stopped: a reply, a run, a task, an image, a draft; the one button word for it | Claude "Retry", GitHub Actions "Re-run" |
| Branch from here | the chat goes on from this message as a branch | ChatGPT "Branch in new chat" |
| More like this | keep the reply as a voice sample, so more replies sound like it | |
| Switch provider | a reply that failed because its provider is busy: the model chooser, then Regenerate on the model you pick | OpenRouter "Switch provider"; plain |
| Keep waiting | a reply that failed because its provider is busy: send it again and wait for the model until Stop | Windows "Keep waiting" (an app that does not respond) |
| Waiting for <model>. | the reply's status while Keep waiting waits for a busy provider | plain |
| Try again | the same, inside a sentence ("Try again later") | ChatGPT |
| Press (a button) | a step inside a sentence names its button with Press ("then press Check"), never Select, Tap or Click | Apple Support "press Return" |
| Stop, Stop the reply | stop the reply; Stop the reply is the Stop button's tooltip | ChatGPT, Claude |
| Reply | its message in a chat; status lines say reply, never answer (The reply failed., The reply took too long and was stopped.) | Gmail, Slack "Reply" |
| Send | send | everyone |
| Message (its name) | the composer's placeholder: its name in its chats, a sub-agent's name in that sub-agent's chat | ChatGPT "Message ChatGPT", Slack "Message #channel" |
| Read 2 notes, ran 4 commands, 2 failed, Activity, Back | a stretch of work between two things the agent says: one row with the server's words, one part per kind of call in the order first seen, and the failed count (skills/words.py `runs`; a non-zero exit counts as failed); its Sheet "Activity" lists every call, a row there opens its call in the same Sheet, Back returns to the stretch | Claude "Ran 2 commands, viewed a file", GitHub Actions "3 failed" |
| Impersonate | it writes your next message as a draft | plain |
| Copy, Edit, Delete | on a message | everyone |
| New messages | the jump to the newest messages | Slack, Discord |
| Activity | the tool calls of one reply, from its "..." | ChatGPT (agent and deep research "Activity") |
| What (its name) saw | what one reply saw (its requests) | |
| Plan, Asks you, Waiting for you, In progress, Done, 2 of 5, done 19:00, since 19:00 | its plan, a button in the chat's head that opens it: its steps, a step where it asks you, it waits on your answer, every step done, the steps done, when a step was done, since when it is on one | Claude Code and Codex "Plan", ChatGPT agent "2 of 5 steps" |
| Daily at 04:00, Sundays at 03:00, Weekdays, Weekends, Every 2 hours from 10:00 to 22:00 | when a Routine's schedule runs, 24h times, from the server (schedules.words); a shape it cannot say stays the cron line | Google Calendar "Daily", "Weekly on Sunday" |
| Waiting for the model | a running Routine run's reason while its work waits for a model (its clock stands still) | plain |
| Started by | a Routine run you or it started by hand, in Events | GitHub Actions "Triggered by" for the same job |
| (Its possessive pronoun) thoughts, Collapse thinking by default | its reasoning, and how its fold starts | Claude "Thinking", ChatGPT "Thought for" |
| Thinking, Writing, Using a tool, Queued | the line while it thinks; a turn's step in lists (lib/format.ts); Queued: it waits for its turn at the model | Claude, ChatGPT |
| Compacted | the chat's older part was summarised | Claude Code "Compacted" |
| Attach | add a file to a message | ChatGPT, Claude |
| Reminders | messages it sends later in this chat | Apple, Google "Reminders" |
| Model, Tools, Pages, Context, Sub-agents | the tabs of the agent's side panel; Pages is also its pages' group in the Tools lists | ChatGPT "Model", "Tools"; Notion "Pages"; Claude Code "/context", LM Studio "Context" |
| Model | which model answers | everyone |
| load: | a note's head line, as the note writes it, in the Context tab's why ("load: off") | Obsidian properties |
| Load automatically, Notes that match each message | the Context tab's switch and its tooltip: each new message brings up the sections of its notes it matches | |
| Waiting for the GPU. | an image batch waits for its GPU (the lease) | ComfyUI, Stable Diffusion WebUI "Waiting" |
| Stopped., Stopped at the tool round limit., The reply reached the length limit., Interrupted., The reply failed. | how a reply ended, where the next line appears: you stopped it, it reached Tool rounds per turn, it reached its length, the server restarted, it failed; a stopped image says Stopped. too | |

### The agent

| Word | In Celine | Same word in |
|---|---|---|
| Name, Pronouns, Colour | who it is, on its Settings page | everyone |
| Choose a model | the first-run step that picks the chat model; also in the side panel while no chat model is set | ChatGPT "Choose a model" |
| Persona | its description of itself | plain |
| Example lines | how it talks, a few lines of its own | plain |
| Banned words | the one list of words its voice leaves out | plain |
| Bias | how strongly the Banned words apply (a quiet default) | OpenAI "logit_bias" |
| Words and phrases the chat models are steered away from. They work on tabbyAPI, llama-swap and llama-server models. | the help of Banned words (its section on the agent's page): the other kinds take no banned strings or word bias | plain |
| Use as portrait, Portrait set | an image becomes its profile picture, and what it says once done | built on Copied |
| Ask (its name) | the agent's draft beside a long text | |
| Bastet | the guard who checks the agent's actions before they run; a sender on the Intercom; the Guard setting | a name like the agent's |
| Created | when it was made; the sort option | Finder "Date Created" |

### Settings and models

| Word | In Celine | Same word in |
|---|---|---|
| General, its name, Features, Guard, Appearance, Notifications, People, Search index, Models, Images & Music, Tools, Email, Vault, Security, Advanced; Sources, App | Settings pages in the list's order, then the list's two headings (Images & Music: ComfyUI and the image and music presets) | Claude "Appearance"; macOS; Bitwarden "Vault"; Firefox, Chrome "Advanced" |
| No limit | a number field that holds 0 where 0 means no limit (Requests per hour, Routine time limit): the field shows the words, emptied it saves 0 | LM Studio, OpenAI "No limit" |
| Turns at once, Tool rounds per turn, Turn time limit, Grace period, Tool time limit, Job time limit, Stream events per chat, Backups kept, Attachments per message, Largest attachment, Largest text read inline, Largest message, Check citations, Search embedding time limit, Busy model retry delay, Busy model max retry delay, Routine time limit, Routines wait for missions, Mission time limit, Sub-agents per mission, Continue missions after Unfreeze, Largest upload, Largest file inside an EPUB or Word file, Largest HTML document, PDF pages read, PDF read time limit, Model requests kept; seconds, minutes, hours, messages, entries, days, bytes, MB, pages | the Advanced page: one number each, its unit beside it, saved on change | plain; LM Studio "Max concurrent", ChatGPT "attachments per message" |
| Prompts, App, Skill guides, Edited | Settings, Advanced, Prompts: every text a model reads, grouped (its name, App, Skill guides, Guard); a row opens the text in the large editor with History and Use the default in its "..."; Edited marks a text you changed | OpenAI Playground "Prompts"; Google Docs "Edited" |
| 3 problems | a source's row in the Settings list while something there needs fixing, in the warn tone; otherwise its state word (Waiting for you, Not connected), Running while it works, nothing while all is well | VS Code "3 problems" |
| Turns, Models, Routines and missions, Files, Storage | the Advanced page's sections in the server's order, each in the index beside them, then one per skill under its label | Firefox, Chrome settings sections; plain |
| Login, Passkey, Password, PIN, This session | the Security list of how you log in, one row each, This session on the one this browser used | Apple, Google "Passkey", "Password", "PIN" |
| Sessions, Log out, Log out everywhere, Last seen, Unknown device | Security: every browser logged in, one row each named by the server ("Firefox on Linux", Unknown device without a user agent), This session on this one and Last seen on the rest; Log out on a row, Log out everywhere for every other one | GitHub "Sessions"; Google "Sign out of all", "Unknown device"; WhatsApp "last seen" |
| Confirm access, Use passkey, Confirm | the dialog adding a way to log in, Reset codes or Create token opens first unless you proved it in the last 5 minutes: your passkey, password or PIN | GitHub sudo mode "Confirm access", "Use passkey", "Confirm" |
| A setting needs a fix, Some settings need a fix | General: the line over the list of settings files and prompts with an error, each with its place and what is wrong | macOS "needs attention"; plain words |
| Source code | General: the link to the code this build runs, its commit on the link | GitHub "Source code"; the AGPL's offer of the source |
| Features | the Settings page of what it has, one switch each; also the onboarding's step | Windows "Optional features", Discord "Features" |
| Core, Built-in, Plugins | the three headings of Features (Settings and Setup): what it is made of (on, no switch), what ships with it, the plugins you add | Drupal "Core", "Modules"; plain |
| Chat, Notebook, Mood and thoughts, Plan, Intercom, Past chats, Bugs, Skills | the Core rows of Features, each with its one line | plain; as their places and skills |
| Next, Back, Skip | the onboarding's steps: on, back one, leave this one as it is | Apple setup, Android setup "Next", "Back", "Skip" |
| Step 4 of 7 | where the onboarding is, with its percentage and a bar | Shopify, Typeform "Step 2 of 5" |
| Decision model, Second look model | Bastet's two models on the Guard Setup step and Settings, Guard: the one that decides (the Decision model role on Settings, Models), the one that looks again; each one's "?" is the server's help (guard/view.py PAGE_HELP) | First look and Second look in Events |
| Decision model (a role) | Settings, Models: Bastet's own role, the models that decide and write its notes and answers (settings/registry.py ROLES); the same name as its pick on Settings, Guard | as Settings, Guard |
| About {thing} | the accessible name of a "?" (Help) after a name: About Context, About Tasks | Apple, Google "About" |
| Address, Check | a service's address and the button that saves and tests it (its Setup step, Settings, Features, Edit; the Vault form too); its help is "The shellbox's address, such as http://shellbox:8770."; an error about it ends with what to do, then press Check | plain; Check as in Check provider |
| Connect a model | the Setup step that saves the first provider and picks its chat model | ChatGPT desktop "Connect", LM Studio "Connect to server" |
| Type, Auto-detect | a provider's type (never kind, also in errors: "The type must be one of"), the first row of its form (Setup, Add provider, a provider's edit dialog); Auto-detect, first and chosen at the start, lets the server say what answers at the address | Windows "Type"; printers, Wi-Fi "Auto-detect" |
| Pick the type if you know it. | the tail of the sentence after a Check with Auto-detect found no known kind | plain |
| http://192.168.1.20:11434, sk-..., Key, Home server | a provider form's placeholders: a value each, never "For example"; sk-... for a hosted kind's key, Key for a local one | everyone's forms |
| Key hash, Copy this into /etc/shellbox/key.hash on the shellbox, then restart it. | the read-only sha256 of the app's key for a box (the shellbox, the browser), with Copy, and its tooltip | GitHub "SHA256 fingerprint"; plain |
| Turns on once the shellbox answers. | the line under a feature switch that cannot be on yet, the service named (the server's `needs`); a click on the switch shows it as a toast | plain |
| Turns on once the shellbox is on., The shellbox is on., The shellbox is off. | the Shares switch: its `needs` while the Shellbox feature is off, and its probe's state line | plain |
| Turn on Shares?, Turn on | the confirm a feature with a warning (Shares) asks before its switch goes on (Settings, Features and the Features Setup step); off needs no confirm | macOS, Windows "Turn on" |
| The link of a share is its key. A page opens only for you while you are logged in, but anyone who gets a link can reach what it shares with a script until it ends (24 hours, or what you set) or you stop it. A public share opens for anyone. | the body of Turn on Shares? (the server's `warn`): the warning, the exception to "no explainer text" (AGENT.md rule 7) | plain |
| Shares | the feature of what it shares from its shellbox (Settings, Features, Setup) | Google Drive "Shared"; plain |
| Import, Importing, Done. 1,234 messages in the archive. | a Discord or WhatsApp Setup step once the account is linked: the first import, its line while it runs (the archive's message count, from the server) and its end; beside it the Messages switch | Google Photos, Apple Photos "Import" |
| Logging in with a user account may break Discord's terms; use it at your own risk. | under Log in with QR code (Settings, Discord and its Setup step) | plain |
| An embedding model lets Search find messages by topic, not only by exact words. A reranker model puts the best matches first. Any OpenAI-compatible server can run them. With Ollama, run ollama pull nomic-embed-text on the server you added in Connect a model, then pick it here. You can change this later in Settings, Models. | the Messages setup form (its Setup step, Settings, Features, Edit) above its Embeddings and Reranking pickers: the one paragraph, the exception to "no explainer text" (AGENT.md rule 7) | plain |
| Music uses the same ComfyUI as Images. | the Music Setup step and form once Images has a ComfyUI address: no second address field | plain |
| (its name)'s computer, Container, Own computer | the Setup step of where the agent works, its name in the title, once Shellbox or (its name)'s Mac is on in Features; Container and Own computer head each one's fields | plain |
| Ready, Not set up, Not connected | a setup step's state at its top (and each card of (its name)'s computer): Ready in the accent with a check, the card framed in the accent; the other two plain, with the reason under them | plain |
| (its name)'s Mac, Run in Terminal on the Mac, Run in a terminal in the Celine folder on this computer, Coding agent, Copy the command above, paste it into Terminal on the Mac and press Return. This page updates when the Mac is connected., Connected: {about}., Set up again, Check, Update, Update available, The Mac is updating. | the Own computer form (the (its name)'s computer Setup step, its Settings page): the setup command (never setup line) with Copy and the waiting line until the Mac reports back, then Connected with the Mac's user, home, chip, memory, macOS and network as the script posted them; Set up again shows the command again; Check; Update runs the app's current setup on the Mac (its badge, read as Update available, while the Mac's setup is older), then The Mac is updating. What the Mac reports (its addresses, its token) is never a field to type | Apple "About This Mac", Bluetooth "Connected", Apple "Set Up Again", macOS Software Update "Update Now" and "Updates Available" |
| The Mac cannot open localhost. First run the command below in a terminal, in the Celine folder on this computer. Then enter this computer's network address, for example http://192.168.1.20:8766, and press Check. The command for the Mac then appears here. | the Own computer form while the app's address is localhost, with the Address field under it | plain |
| The Mac is not connected yet., Not connected yet. Run the command above in Terminal on the Mac. | what (its name)'s Mac lacks until the Mac reports back: Features, All set, Check's lines, the agent's settings | plain |
| Screen: 26 tools, Shell: Starting, Accessibility is off: no clicks or typing., On the Mac, open System Settings, Privacy & Security, and turn on Celine under Accessibility. | Check's lines on the Mac's form, one line each: each server's tool count or what kept it down, and each macOS permission the screen server lacks with what it gives the agent and its pane (also after Connected in the form's note) | plain; System Settings' own pane names |
| Anything worth knowing about you | the placeholder of About you | plain |
| All set, {name} is ready., On:, Off:, Off because they did not answer: {features}. Check their addresses in Settings, Features., Not set up: {feature}. {why}, You can change all of this later in Settings. | the last Setup step: its title, then the features on, the ones switched on whose service did not answer, one line per one switched on that is not set up with what is missing, and the rest off, as muted lines, then that Settings changes all of it | Apple setup "All set", Google "All set"; plain |
| Guard mode, Warn, Enforce | Settings, Guard: how Bastet acts while her feature is on (off is the feature switch): Warn tells you what she would have done, Enforce does it | Kubernetes Pod Security "warn", "enforce" |
| the "?" texts of Mode, Decision model, Second look model, Rules and Exceptions | Settings, Guard: the server's (guard/view.py PAGE_HELP, names filled in; GET /api/guard/rules `help`) | plain |
| Rules, Off, Warn, Stop, Freeze | Settings, Guard: what each rule does when it finds something (Off its switch, else Warn, Stop or Freeze) | ESLint "off", "warn"; Freeze as Bastet says it |
| Sandbox escape, Prompt injection, Data exfiltration, Destructive tool calls, Typing on other sites, Bypassing a stop, Too many stopped tool calls, Injected instructions | Bastet's rules as Settings, Guard, Events and Bastet's own lines name them (guard/rules.py `label` and `plain`): a way out of the shellbox; a tool call that carries out instructions from text someone else wrote (indirect prompt injection); private data sent out; a destructive tool call you did not ask for; typing on a site outside the task; the same stopped tool call tried another way; the stop count of one turn; the page check's mark on outside text written to an AI | OWASP LLM01 "Prompt injection", LLM02 "Sensitive information disclosure"; MITRE ATT&CK "Exfiltration", "Escape to host" |
| Exceptions, End | Settings, Guard, shown only while an exception holds: each exception with where and until when, and the matched text it lets through; End ends it now | Windows Security "Exclusions", firewall "exceptions"; Zoom "End" |
| Waiting for Bastet., Waiting for Bastet. {why} | a step's line while Bastet checks it, and while her decision model cannot answer, with the server's why once per new why (guard/flow.py `_until`) | as Waiting for {model} |
| No chat model yet. Bastet needs one to decide., Connect a model, Open Models | the Guard form when no saved provider offers a chat model: the line and the way to one (the Setup step in the onboarding, Settings, Models elsewhere) | plain, as Open Guard |
| {provider}: Connected, Not connected, Not checked, Not set up; This model is no longer on its provider. Pick another one. | the state under a picked Decision or Second look model: its provider's, as on Models, or that the model is gone | as on Models |
| Persona, Replies to (its name), Messages to you | Settings, Guard: Bastet's three long texts (who she is, how she answers the agent on the Intercom, how she writes her messages to you), each with Edit, History, Ask (its name) and Use the default | Persona as on the agent's page; plain |
| Make her warmer, Make her replies shorter, Make her notes plainer | what to ask the agent in the editor of Bastet's texts | as the hints of the agent's own texts |
| Open Guard | the Features row's link to the Guard page, where Bastet is set up | plain |
| Open Images & Music | the Images and Music Features rows' link to their page while they are on | plain, as Open Guard |
| ComfyUI, Images, Music, On, Make default | Images & Music: the server's section, a section per kind, a preset's switch (it uses it), and the menu item that makes it the first of its role | ComfyUI's own name; macOS "Make Default" |
| Upload workflow, Replace workflow, Download workflow, Reset to defaults | a preset's workflow: a new preset from a ComfyUI workflow in API format, another workflow for one, the workflow with its placeholders as a file, an edited shipped preset back as it came | built on Upload and Download; ComfyUI "Workflow"; Reset to defaults as on Appearance |
| Name, Family, Good at, Prompting, Example, Variants, Variant, Size step, Largest image, Models, LoRAs, LoRAs per image, LoRA chain after, Sampler node, Trigger words node, Full model steps, Full model CFG, Full model sampler, Full model scheduler; pixels, megapixels | a preset's settings on Images & Music, each saved on change; the units beside them | ComfyUI, A1111 "Sampler", "Scheduler", "CFG"; Civitai "LoRA" |
| Reset codes | new recovery codes, the old ones stop working | Google "Get new codes" said plainly |
| API tokens, Create token | named tokens for scripts that call the API, and the button that makes one | GitHub "Personal access tokens" |
| You | the section of your name, time zone and about you (General, the onboarding) | Apple "You" in Contacts and Health |
| Network, Address | General: the section of the app's own address (logins, share links and the Mac's setup command use it) | macOS System Settings "Network"; Address as for a service |
| Behaviour | the section of the agent's page for how it works (Ask before setting up services, Ask before cloud models from your lists, Web search for the coding agent) | Discord, Firefox "Behaviour" sections |
| Show notifications, Replies, Intercom, Missions, Routines, Approvals | Notifications: the switch for all of them, one per kind (the agent's replies in a chat, an Intercom message to you, a mission waiting for you, a failed run, a Vault or Bastet request) | Chrome "Show notifications"; Slack notification preferences |
| Push, Push notifications, This device, Remove | Notifications: this device's switch for pushes while no page of the app is open and focused, the list of every device that takes them (named by the server like Sessions), This device on this one, Remove on a row | Web Push; Chrome "Push notifications"; Security "This session" |
| Allowed, Blocked | the browser's notification permission (Notifications) | Chrome site settings "Allowed", "Blocked" |
| Blocked in this browser, Open Settings, Allow them in this site's settings in your browser., Notifications only work when the app opens over https., Notifications need a browser that offers them. | Notifications: a permission revoked (the toast and the page), a page on plain http, a browser without them | plain |
| Hide thinking, Hide tool calls | Appearance, Chat: leave its thoughts and its tool calls out of the chat | Claude "Show thinking" |
| Accent colour, Text size | Appearance | macOS "Accent color", iOS "Text Size" |
| Face colour | the agent's colour on its page and in the first steps: its face while it has no portrait, and in Talk; Accent colour paints the app | plain |
| Colour, Background, Chat | the three sections of Appearance: the colours, the canvas behind everything, how a chat reads | Windows "Personalisation: Colours, Background"; plain |
| Tint, How much the accent colours the background and surfaces | Appearance, Colour: how strongly the accent hue colours the greys (0 % plain grey), and its help | macOS "wallpaper tinting"; photo editors "Tint" |
| Contrast, The step in lightness between the background and surfaces | Appearance, Colour: how far apart the surfaces are in lightness, and its help | macOS and Windows "Contrast" |
| Grain, Fine, Coarse, Paper, None, Opacity | Appearance, Background: the canvas's grain pattern (None: a plain canvas) and how much of it shows | Photoshop "Grain", "Opacity"; Procreate "Grain" |
| Reset to defaults, Reset the appearance?, Reset | Appearance: every choice back to the server's default, its confirm and the confirm's button | macOS, Windows, VS Code "Reset to defaults" |
| Rose, Peach, Sage, Teal, Sky, Violet, Miku | the colours: Accent colour (Appearance) and its Face colour; Miku is a bright teal blue | macOS accent colour names; Miku after Hatsune Miku's hair |
| Roles, Chat, Tasks, Writing, Titles and descriptions, Decision model, Embeddings, Reranking, Photo descriptions, Browser screenshots, Image generation, Music generation, Dictation, Read aloud | the roles on Settings, Models: what each list of models is used for (settings/registry.py ROLES) | plain |
| Requests at once, Timeout, Thinking, Service tier, Data collection, Require parameters, Allow fallbacks, Sort by, Shares its GPU with | a provider's options in its dialog, beside Requests per hour and Next model at the hourly limit (settings/registry.py) | OpenRouter provider routing; LM Studio |
| Loaded, Loading, Not loaded, Not loaded, loads in about 12 s | model memory; Loading while its server says it is starting (the chat's "Loading <model>.") | Ollama "loaded" |
| Context size, Server: {n}, differs from the server | a model's written context and what its server says it runs with (the server's number counts) | LM Studio "Context Length"; Open WebUI "Context Length"; plain |
| Unload after | how long an idle model stays loaded | plain; Ollama says `keep_alive`, Open WebUI "Keep Alive" |
| Cloud | a provider's switch under Address (its dialog, Add provider): on, its models run on a server outside your network and a model of it the agent picks itself asks you first; preset from the address (LAN and Tailscale addresses are local), neither on nor off until the server has said | OpenRouter, Anthropic: cloud providers; plain |
| Requests per hour, {provider} reached its {n} requests this hour. | a provider's option: the most requests it gets in one hour, counted since the app started (0, no limit); past it a reply fails with the sentence and other work moves to the next model in its list, or waits, as Next model at the hourly limit says | OpenAI, OpenRouter "rate limits", "requests per minute" |
| Next model at the hourly limit | a provider's option beside Requests per hour: on (the default), work past Requests per hour moves to the next model in its list; off, it waits for this provider | LiteLLM "fallbacks" on rate limits |
| Ask before cloud models from your lists | on the agent's page, Behaviour: on, a cloud model from your role lists asks you too before a sub-agent, mission or coding task runs on it (off by default) | Cursor "Ask before running" |
| Searches per hour, Web search reached its {n} searches this hour. | Advanced, Web: the most web searches in one hour, counted since the app started (0, no limit), and the refusal past it | plain; SearXNG "limiter" |
| Read-only, Can edit | sub-agent access | Google Docs "Can edit" |
| Rename account, Separate account, Mark {n} as the same person, Also called, About {name} | People: an account's menu, accounts made one person, a person's other names, the notes about a person | Google Contacts "Merge", "Also known as" |
| Device | where a drone runs: one of the agent's devices (mac), next to Agent CLI on a sub-agent | Apple, Google "Device" |
| Instructions | a mission's task text | ChatGPT and Claude Projects "Instructions" |
| Minimum time, Runs at least until | how long a mission runs at least before it may close as done, in its form and on its page | plain |
| Check-in, Every 30 min | how often a mission sends where it stands (a notice to you, or a report to whoever it reports to), in its form and on its page | plain |
| Result, Results | what a mission or sub-agent sends back | Apple Shortcuts "Show Result", Google "Results" |
| Trigger, Triggers, Folder, Event | what starts a run: changes in a folder, or an event of a source | Zapier, GitHub Actions |
| Schedule, Runs after, Reviewer, Turns | Routines | GitHub Actions "schedule" |
| No runs | a day in Routines with nothing in it | GitHub Actions "no workflow runs" |
| When a run fails, Retry, Skip | a schedule's choice in Limits: Retry runs a failed run again for the same day, 15 minutes after it failed, then 1 hour and 4 hours after each failed retry, never past the next run; Skip ends it Skipped with no notice, and its day is marked as one with a failed run; a day with a run that waits for its model is marked too, and the first such run of a day sends one notice | GitHub Actions "Re-run jobs", "Skipped" |
| Retry, Retry of, Retried | on a failed, skipped or stopped run's page: run it again now; the facts that link a retry and the run it retries | GitHub Actions "Re-run", "Attempt #2" |
| Only when the app is idle, Time limit | a schedule's Limits: it starts only while nobody uses the app; the most minutes a run may work | plain |
| Cron | only the label of a cron expression field | cron, Vercel "Cron Jobs" |
| Take control | you take its browser | OpenAI Operator "Take control" |
| Return control | you give its browser back | plain; Operator names only "Take control" |
| Extract text | read a document's text again | |
| Running, Queued, Waiting for you, Failed, Done, Scheduled, Not set up | states; Queued for a run or a work item that has not started; Scheduled for a Routine that runs later; Not set up for a tool group or a feature that lacks its setup | GitHub Actions, everyone |
| Invalid | a citation in its reply that failed the check, shown with its id | everyone |
| Waiting, Waiting for you; Approvals, Missions, Sources, Intercom | the rail item while something waits for you, and its page (/waiting): the agent's requests, missions that ask you, chat sources whose login was lost and Intercom threads, each group under its head with its count (waits.py GROUPS); a row opens the page that answers it; a mission you stop leaves it | Slack "Activity", GitHub "Inbox" |
| Dismiss, Dismiss all | a message that waits for you leaves the waiting list: an Intercom thread, or what a mission wrote you (Waiting for you, the mission's page); Dismiss all is in the "..." of the Intercom group on Waiting for you | Android notifications |
| Mark as done, Started from | what a mission asks you (Waiting for you, the mission's page): Mark as done ends an open mission as done and stops its work; Started from names the chat or run that started it | GitHub "Mark as done" |
| Close | the X that hides a notice, a toast or a row | everyone |
| Continue in a chat | an Intercom thread goes on as a new chat with it, its messages carried over | plain |
| Go to, Show keyboard shortcuts, Keyboard shortcuts | the Ctrl+K switcher (its field says Search); the ? key and the sheet it opens | VS Code "Go to File", Gmail "Keyboard shortcuts" |
| Guides | its skill guides in the Notebook | |
| Save, Cancel, Close, Add | | everyone |
| No connection to the server, Reconnecting | the block over the app while the server is gone | Slack "Reconnecting" |
| Transcript, Available voices | the speech test: what dictation heard, the voices its server lists | YouTube "Transcript" |
| Dictate, Stop recording, Cancel dictation, Read aloud, Stop, Speech options, Send when done, Read new replies aloud | the speech controls: start dictation, end it (its text lands in the box), drop it; play speech, stop it; the options sheet and its switches | ChatGPT "Read aloud", "Stop" |
| Shares | the Pages tab's heading over what it shares that runs (a server in its shellbox, a file or folder of its home or a device) | plain; Google Drive "Shared with me" |
| Port 8000 in the shellbox, ~/out/clip.mp4 on (device), Not running, Ended | a share card's line from the server: what is shared and from where while it answers, else why not | Docker Desktop "Not running"; plain |
| Public | the tooltip of a share card's globe: anyone with the link opens it (else only you, logged in) | Google Drive "Public on the web" |
| Stop sharing | the icon beside a share card: ends the share at once, for it too | Google Meet, Zoom "Stop sharing" |
| Nothing is running on port 8000 in the shellbox right now., The shellbox is not answering right now., Not found | the page a share opens to when its server, its box or the share is not there | plain |

### Tools

The agent's skills as Settings, Tools and the side panel's Tools tab name them
(`server/celine/settings/tools_meta.py`), each a place word where there is one. A tool is
its skill's word, a colon and what it does in plain words ("Messages: search", "Terminals:
run a command"); the verbs are the everyday ones (search, read, list, write, edit, add,
set, stop, check). The Where filter names the places: its name, Missions, Routines.

| Word | In Celine | Same word in |
|---|---|---|
| Messages | it searches and reads the WhatsApp and Discord archive | the Messages place |
| Notebook | its own notes | the Notebook place |
| Documents | your files | the Documents place |
| Past chats | your earlier chats with it | ChatGPT "Reference chat history" for the same job |
| Terminals | its terminals in its shellbox | the Terminals place |
| Code | its coding agent in its shellbox | VS Code, Cursor "Agent" |
| Intercom | its messages and threads | the Intercom place |
| Missions | the missions it works on | the Missions place |
| Plan | its plan, from the button in the chat's head | as above |
| Routines | its schedules and their runs | the Routines place |
| Web | it searches the web and reads pages | ChatGPT, Claude "Web search" |
| Email | its inbox; one message is an email, several are emails (never a mail, mails) | everyone |
| Browser | its browser | the Browser place |
| Vault | its passwords | Bitwarden "Vault" |
| Pages | its pages; also the feature's name in Features (its id stays artifacts) | as above |
| Image generation, Music generation | it makes images and tracks with ComfyUI | Midjourney, Suno |
| Models | the models it may ask | as above |
| Writing | it writes a post or a message in its own voice | Grammarly, Notion AI "Writing" |
| Skills | it loads a skill's tools and reads its guide | Claude "Skills" |
| Bugs | the problems with the app it files for you | as in Events |
| Portrait | its profile picture | as in "Use as portrait" |
| Voice notes | its voice notes to you, spoken by the Read aloud model | WhatsApp, Telegram "voice message" for the same job |
| Persona | its description of itself | as above |
| Settings | it reads every setting and proposes changes you approve | the Settings place |
| Mood and thoughts, Mood, Status, Thoughts | its mood, the emojis by its name (Set the status), the thoughts it writes down | Slack "Set a status"; Daylio "Mood" |
| Sub-agents | the helpers it gives instructions to; also the side panel's tab with every sub-agent of the chain, each under the one that started it | Claude Code "Sub-agents" |
| Sub-agent, Mission, Folder, Step, Model, Provider; Instructions, Task, Messages | the facts of a request to use a cloud model the agent picked itself (the sub-agent, mission, coding folder or plan step, then the model and the provider by their labels), and Details: the whole text it gets | OpenRouter, Anthropic: cloud providers; Claude Code "Sub-agents" |
| Discord, WhatsApp | read live from your accounts | their own names |
| Require approval, Add, Search tools, Remove | Settings, Tools: the tools whose every call waits for your answer, the guard on or off, each by its skill and its name; Add picks one from every tool, its search finds it by either name; Remove takes it off | GitHub "Require approval" |

### Events

The words of the Events place (`server/celine/events.py` gives them).

| Word | In Celine | Same word in |
|---|---|---|
| Guard, (its name), Background | the tabs: what Bastet decided, what the agent did in its chats, what ran without you (runs, tasks, missions, sub-agents, jobs); Guard is also the Intercom tab of Bastet's threads | Guard as the setting; Background as in the transcript |
| Turn, Turns, Tools, Notebook, Background jobs, Intercom, Runs, Decisions, Freezes, Exceptions, Titles, moods and descriptions, Persona, Bugs | the Type filter, and a turn's row; Persona: the agent's own edits of its persona and Example lines | Turns as in Routines; Tools as in the side panel; GitHub Actions "workflow runs"; Intercom as the place; Freeze as Bastet says it; Exceptions as Windows Security says it |
| Title, Mood, Document descriptions | the items of Titles, moods and descriptions: a chat's title, its mood after a turn, the documents' descriptions | plain |
| Bastet | the guard's decisions (her name) | |
| Wrote, Edited, Deleted, Moved ... to ... | its notebook writes ("Wrote people/noor.md") | Google Docs activity "edited", "moved" |
| To (the user's name) | a message it sent you | Gmail "To" |
| Stopped, Skipped, Cancelled, Scheduled, Sent | states besides Running, Done, Failed and Paused. | GitHub Actions "Skipped", "Cancelled"; Gmail "Scheduled", "Sent" |
| Allowed, Stopped, Frozen, Found, Approved, Denied, No answer, Allowed by exception | Bastet's decision on a call, the words of her banners | Android permissions "Allowed", "Denied" |
| Would have stopped, Would have frozen | what Bastet would have done while she only warns, as her banners say it | |
| Freeze, Unfreeze, Exception for (a rule), Queued | a freeze and its end, an exception you gave, a work item that waits | the words above; Queued as in the transcript |
| Could not decide | a decision Bastet could not make (her model gave no answer: it was down, timed out or the call did not fit), counted apart from Failed | |
| Decision, Rule, Mode, Enforce, Warn | Bastet's filters: what she decided, by which rule, and whether she acted (Enforce) or only warned (Warn) | GitHub code scanning "Rule"; as Bastet's setting |
| Details | open one decision of Bastet's from its row | macOS Finder, Windows "Details" |
| First look, Second look, Flagged, Not flagged | one decision's chain: the decision model's decision and why, then the big model's second look (guard/chain.py) | a second opinion, as people say it; OpenAI Moderation "flagged" |
| Question, Score, Model, Time, Source, Quote, Matched text, Arguments | one decision's facts: what its decision model was asked, the score it gave, which model, how long, where the text came from, what it quoted, the text the rule matched, the call's arguments | Purview "matched content"; OpenAI evals "score"; Chrome DevTools "Time" |
| Allow once, Allow for 7 days | answer a stop or a freeze, on its banner or from its row: the same call runs once; tool calls with the same finding run in that chat for 7 days (an exception, listed under Exceptions). Either wakes the agent with Bastet's line, so it makes the tool call again now | Chrome site permissions "Allow this time", "Always allow" |
| Open, Closed, Close, High, Medium, Low | a bug it filed: open until you close it (Close on its Intercom thread); its severity, beside Open in its row ("Open, high") | GitHub Issues "Open", "Closed", "Close issue"; Jira priority "High", "Medium", "Low" |
| No events yet, No events match | the empty list, and a list the filters empty | plain |

### Actions

| Word | In Celine | Same word in |
|---|---|---|
| Add provider, Add model, Add models, Add passkey, Add password, Add PIN | add one of these | built on Add; Bitwarden "Add passkey", LM Studio "Add model"; Windows "Add PIN" |
| Allow notifications, Not now | the browser's notification ask, and saying no to it; Not now also hides the Vault banner for a day | Slack, Chrome "Not now" |
| Apply | use the picked settings or filter | everyone |
| Change, Change model | pick another value or model | ChatGPT "Change model" |
| Default, <model> | the model chooser's first row, and the Model row while none is picked: the model a chat (or a mission) comes to without its own | ChatGPT "Default" model; macOS "Default" |
| Check and add, Check email, Check provider, Checking | test a connection (and add it when it works); Checking while it runs | Thunderbird "Check mail", Open WebUI "Verify connection" |
| Check again | ask a feature's probe anew (the Features page, the onboarding); read the browser's notification permission anew (Notifications) | Chrome "Check again" (connectivity), macOS Software Update "Check Again" |
| Choose channels, Choose chats | pick what the connection reads (WhatsApp and Discord chats: their apps' word stays) | WhatsApp "Choose chats" |
| Clear recent searches | empty the recent searches | Google, YouTube |
| Close tab, Close terminal | close one of its tabs or terminals | Chrome "Close tab", VS Code "Close terminal" |
| Copy link, Copy message link, Copy prompt | copy one of these | Slack "Copy link", Discord "Copy Message Link" |
| Approve, Deny | say yes or no to a request (its card in the chat, on the Vault page and in Waiting for you) | macOS, Android permissions |
| Details | a request's long text, whole, in a sheet (Instructions, Arguments, the text of an edit) | macOS Finder, Windows "Details" |
| Entry, Sites, File, Reason, Read right before | a request's facts: the vault entry, its sites, the file in the shellbox, the agent's reason in its words, the last tool that read text from outside before it asked | Bitwarden "Entry"; plain |
| Waiting, Waiting for you | the rail item, shown with its count only while something waits on you, and its page: every request as its card, every mission that asks you, every lost login and every Intercom thread that waits | GitHub "Waiting for your review" |
| Mission (name), Routine (name), Chat (title), Background task (title) | where a request's work lives, on its card in Waiting for you | the places' own words |
| {n} waiting, Waiting for you | an Intercom thread row and a message in it that wait for you, in the warn tone | GitHub "Waiting for your review" |
| Approved., Denied., No answer. | a request's notice once it closed, replacing the one that asked | Android permissions "Allowed", "Denied" |
| Paused | the rail item while no model of the agent's answers (it opens Settings, Models), and the mini player; never a reply or an image | plain |
| Settings, Change {target}., Default, Empty, On, Off, Saved, Not set, New value, Removed | the agent's request to change Settings in a chat: the kind's name, its line, then each field's before and after (a secret by its state only) | macOS "Change Settings"; Bitwarden "Saved" |
| Freeze, Unfreeze, Frozen | stop everything the agent does until you let it go on (the guard, or you by hand); Frozen in the rail, which opens the confirm Unfreeze? with why it froze, and Unfreeze on Bastet's banner | Revolut, Monzo "Freeze card" |
| Go to the original message | jump to the message a reply or result came from | Slack, Discord "Jump to message" |
| Lock, Unlock, Client ID | the Vault; the API key's Client ID in its form | Bitwarden "Lock", "Unlock", "client_id" |
| Log in with QR code | link WhatsApp or Discord with the phone | Discord "Log in with QR Code" |
| Move up, Move down | the order of a list (models) | Google Docs, Word |
| Move ended terminals to Trash | the ended terminals in one go | built on Move to Trash |
| Ended today, Ended yesterday, Ended 12 Mar | the heads that group ended terminals by day | Slack "Today", "Yesterday"; Gmail |
| Today, Yesterday, Previous 7 days, Previous 30 days, March, 2025 | the heads that group chats by their last activity: the month this year, the year before | ChatGPT's chat history |
| Close sidebar, Open sidebar | fold the chat list away to the left and bring it back | ChatGPT "Close sidebar", "Open sidebar" |
| Open, Act, Scroll, Read, Close | the names of its browser tools, in the History's filter and the manual call (Read is what it did when it looked at the page) | automation "open", "click", "scroll"; browser "Reader" |
| 1 screenshot, 3 screenshots | the pictures a browser call took | macOS, Windows "Screenshot" |
| Chat | the title of a mission's or a sub-agent's chat page (the Open chat link opens it) | ChatGPT, Claude "chat" |
| Open Browser, Open mission | a link that names where it goes | VS Code "Open Settings" |
| Remove key, Remove current Discord token | take a stored key or token out | Bitwarden "Remove" |
| Saved, Remove | a stored secret in a form: it is there, never shown; take it out | Bitwarden "Remove"; Chrome passwords "Saved" |
| Save key | keep a stored value | GitHub "Save" |
| Set new Discord token | put in a new token | GitHub "Regenerate token" |
| Reply, Reply to Bastet | answer a message; answer Bastet in her banner's thread | Gmail, Slack; built on Reply like Intercom's "Reply to" a sender |
| Restart | start a stopped thing again (a terminal, a server) | Windows, VS Code |
| Start mission | start a mission | plain |
| Sync now | sync at once | Google Drive, Thunderbird |
| Upload folder | upload a whole folder | Google Drive "Folder upload" |
| Use the default, Use their own name | a choice in a setting | macOS "Use default", Discord nicknames |
| Variables | the values a prompt fills in | Postman, n8n "Variables" |

Also in use: Favourites, Unread, Play, Pause, Copied,
Variants, Creating image, Notifications (rows in the tables above).

## Banned words

"Match": `label` bans the whole UI string (trimmed, any case), so a button labelled
"Again" is banned while "Try again" is not; `word` bans the word or phrase anywhere in a UI
string as a whole word (any case), so "Filter chats" is banned while "Filters" is not.

| Banned | Match | Use instead | Why |
|---|---|---|---|
| Again | label | Regenerate | ChatGPT, Claude |
| Write it again | label | Regenerate | same job |
| Run again | label | Run now | plain |
| What-if, what-ifs, What-if from here | word | Branch, branches, Branch from here | ChatGPT |
| Filter | word | Search | plain |
| How hard, Strength | label | Bias, as a quiet default of Banned words | Bias |
| Never use, Avoid, Wording, Words it never uses, Words she never uses | label | Banned words | one list, one clear thing |
| Lately | label | Recent | ChatGPT, Claude |
| Tweak | word | Edit | everyone |
| Shipped list | word | Defaults | everyone |
| Made | label | Created | Finder |
| Start values | word | Defaults | plain |
| Brief, Briefs | word | Instructions | ChatGPT and Claude Projects |
| Report, Reports | word | Result, Results | plain |
| Started by events | word | Triggers | Zapier, GitHub Actions |
| Working, Busy | label | Running | one word for the state; GitHub Actions |
| Needs setup | word | Not set up | one word for the state |
| Everyone working | word | Everyone | the Intercom recipient, plain |
| Artifacts | word | Pages | the side panel's word |
| Needs you | word | Waiting for you | plain |
| Follows | label | Runs after | plain |
| Partner, Partner prompt | word | Reviewer, Reviewer prompt | plain |
| Rounds | label | Turns | ChatGPT |
| Kind | label | Type | everyone |
| New text | label | New messages | Slack, Discord |
| Make this the newest | label | Restore this version | Google Docs, Claude |
| Extract text again | label | Extract text | plain |
| Take over | label | Take control | OpenAI Operator |
| Give back | label | Return control | plain |
| Loads first | word | Not loaded | Ollama |
| Unload when idle | label | Unload after | plain |
| Reads | label | Read-only | Google Docs |
| Works | label | Can edit | Google Docs |
| Chunked | word | Passages | plain |
| Full file | word | Whole file | plain |
| Click here | word | a link that names its target | explainer text |
| Pass on this | label | Allow for 7 days | "pass on" means decline |
| Try again | label | Retry | the button word; Try again only inside a sentence |
| Save and resend | label | Send | one verb |
| Revoke | label | Remove | one word for taking a stored thing out |
| Next model when full | label | Next model at the hourly limit | "full" has more than one reading |
| Dismiss for a day | label | Not now | Dismiss is the Intercom's |
| Shortcuts | label | Keyboard shortcuts | plain |
| Reasoning effort | label | Thinking | one label for every provider type |
| Add a provider | label | Add provider | as Add model, Add passkey |
| Every day | label | Daily | as the schedule's own words |
| New conversation, Open conversation, Past conversations, Conversations | label | New chat, Open chat, Past chats, Chats | the owner's word, as ChatGPT and Claude |
| Watch | label | Warn (Bastet), Folder (a trigger) | the owner: "neither, make it nicer" (2026-10-23) |
| Unlink account | label | Separate account | the owner's pick (2026-09-24) |

A runtime string counts as it shows: "Compacted. Click here to view new Context" became "Compacted" with an Open link.

## The block

The banned words again, one per line: `label|word <banned> => <replacement>`. Lines
starting with `#` are comments. Matching ignores case. tests/test_words.py checks the web app's strings
(a `label` against a whole string, a `word` inside one) and the server's labels against it.

```words-banned
# label: the whole UI string equals the banned text
# word: the banned text appears as a whole word or phrase anywhere in a UI string
label Again => Regenerate
label Write it again => Regenerate
label Run again => Run now
word What-if from here => Branch from here
word what-ifs => branches
word what-if => Branch
word Filter => Search
label How hard => Bias
label Strength => Bias
label Never use => Banned words
label Avoid => Banned words
label Wording => Banned words
label Lately => Recent
word Tweak => Edit
word Shipped list => Defaults
label Made => Created
word Start values => Defaults
word Briefs => Instructions
word Brief => Instructions
word Reports => Results
word Report => Result
word Started by events => Triggers
label Working => Running
label Busy => Running
word Needs setup => Not set up
word Everyone working => Everyone
word Artifacts => Pages
word Needs you => Waiting for you
label Follows => Runs after
word Partner prompt => Reviewer prompt
word Partner => Reviewer
label Rounds => Turns
label Kind => Type
label New text => New messages
label Make this the newest => Restore this version
label Extract text again => Extract text
label Take over => Take control
label Give back => Return control
word Loads first => Not loaded
label Unload when idle => Unload after
label Reads => Read-only
label Works => Can edit
word Chunked => Passages
word Full file => Whole file
word Click here => a link that names its target
label Words she never uses => Banned words
label Words it never uses => Banned words
label Pass on this => Allow for 7 days
label Try again => Retry
label Save and resend => Send
label Revoke => Remove
label New conversation => New chat
label Open conversation => Open chat
label Past conversations => Past chats
label Conversations => Chats
label Conversation => Chat
label Search conversations => Search chats
label Next model when full => Next model at the hourly limit
label Dismiss for a day => Not now
label Unlink account => Separate account
label Watch => Warn
label Shortcuts => Keyboard shortcuts
label Reasoning effort => Thinking
label Add a provider => Add provider
label Every day => Daily
```
