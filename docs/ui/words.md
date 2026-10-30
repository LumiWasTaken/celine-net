# Words

Every word on screen is in the table below, is plain content (a name, a message,
a title someone wrote), or is an icon with a tooltip from this table. The `words-banned`
block at the end of this file lists the words no UI string uses.

## Rules

1. One word per job, the same in every place. "Search" is always Search.
2. Only standard words: what ChatGPT, Claude, Apple or Google
   call the same thing. A new word goes into the table first, with who else uses
   it, before it goes into the code.
3. Sentence case; British spelling ("colour"); counts as "3 conversations"; dates en-GB.

## Approved words

"Same word in" names apps that use it for the same thing.

### Places

| Word | In Celine | Same word in |
|---|---|---|
| Celine | the chat with it (the place takes its name) | ChatGPT, Claude: the chat is the home |
| Missions | long tasks it works on | |
| Routines | its schedules and their runs | Google Home, Alexa "Routines" |
| Notebook | its own notes | ChatGPT "Memory" for the same job |
| Intercom | messages between it, missions and sub-agents | |
| Documents | your files | Google Docs, Apple Files |
| Messages | the WhatsApp and Discord archive ("chat" means a chat with it). Inside it a WhatsApp or Discord chat keeps its app's word, chat (Search chats, Choose chats) | Apple "Messages", Google "Messages" |
| Terminals | its terminals | macOS Terminal |
| Browser | its browser | |
| Events | everything it did and everything that ran, one list | GitHub "Events", Google Cloud "Events"; Discord "Audit Log" for the same job |
| Search | the archive search, and every search box | ChatGPT "Search chats", Claude "Search" |
| Settings | settings | everyone |
| Setup | the onboarding (/welcome), run again from the top of the Settings list | macOS "Setup Assistant", Windows "Setup" |
| Design your agent | the first setup step: name, pronouns, colour, persona, example lines; its name is whatever the user typed, never a fixed word | character creators |
| Gallery | every image and track  | Android Gallery; ChatGPT "Library" |
| Studio | make an image or music  | Google AI Studio |

### Lists and items

| Word | In Celine | Same word in |
|---|---|---|
| New conversation, New mission, New note, New folder | the one primary New X in a head | ChatGPT, Claude "New chat"; Apple "New Note" |
| Search | the search field and its placeholder | everyone |
| Sort: Recent, Name, Created | sort order, in the Toolbar | Finder "Sort By", Google Drive |
| Group: Folder, Person, State, None | grouping, in the Toolbar | Finder "Group By" |
| Folder, Person | the Toolbar's filters | |
| Date: Today, This week, This month, This year | the date filter of Search, in the Toolbar | Gmail, Google Drive "Modified" |
| All messages | every source's chats in one list, in Messages | Gmail "All Mail" for the same job |
| Select, Select all, Done | selecting many | Apple Photos, Mail |
| Archive, Archive older than | move out of the list, kept | ChatGPT "Archive", Gmail |
| Restore | back from the Archive or the Trash | Google Drive, Windows |
| Trash, Move to Trash | the bin and moving there | macOS, Gmail, Google Drive |
| Delete permanently | only inside the Trash | Google Drive "Delete forever", Outlook "Permanently delete" |
| Delete | a message, a note version (menus) | everyone |
| Open | open the item | everyone |
| Open Settings, Open Setup, Open conversation, Open terminal | a link that leaves names where it goes (Open Setup: the feature's own Setup step while it is on) | VS Code "Open Settings", "Open in Terminal" |
| Back to Settings | the way out of Setup run again from Settings, never on the first run | Gmail "Back to Inbox" |
| Undo | take back a reversible action from its toast (Archive one item); never a delete or a move to Trash, those ask first | Gmail "Undo", everyone |
| Unsaved, Unsaved changes | an edit not saved yet, in an editor with Save (a note, a document); Settings save themselves | Google Docs, VS Code "Unsaved changes" |
| Rename, Edit, Duplicate, Copy | | ChatGPT, Claude, Finder |
| Pin, Unpin | keep at the top | Slack, Discord, ChatGPT |
| Export, Download, Upload | | everyone |
| Favourites, Add to Favourites, Remove from Favourites | pictures and tracks kept as favourites (a Toolbar toggle, the heart) | Google Photos, Apple Photos "Favourites" |
| Unread | a notification or message you have not seen (a dot, a count) | Gmail, Slack |
| Direct messages | the Intercom Type filter for plain messages (beside Instructions, Results, Jobs, Notices, Triggers); not Messages, which is the archive place | Slack "Direct messages", Discord "Direct Messages" |
| Play, Pause | the mini player of a track | everyone |
| Copied | what Copy says once it has copied | ChatGPT, Claude "Copied" |
| Variants, Variant | the pictures made for one picture block, pick one of four | Leonardo "Variations" |
| Creating image | a picture on its way | ChatGPT "Creating image" |
| Notifications | the notification center in the rail; the Settings page of the system notifications while the tab is hidden | everyone; Chrome, macOS "Notifications" settings |
| Show more | the end of a long list | Google |
| More | the "..." menu and its tooltip | Google "More" |
| Clear search | empty the search field | Gmail |
| Recent | recent items | ChatGPT, Claude "Recents" |
| Type | what kind of file it is (a filter, a field) | Google Drive "Type" filter, Windows Explorer "Type" |
| Book, Books | an EPUB document and its type | Apple Books |
| Versions, History, Previous version, Next version | earlier versions | Google Docs "Version history"; ChatGPT version arrows |
| Restore this version | make an older version the current one | Google Docs, Claude artifacts |

### Messages and turns

| Word | In Celine | Same word in |
|---|---|---|
| Regenerate | a new answer in place of this one | ChatGPT, Claude |
| Continue | write on from where it stopped | ChatGPT "Continue generating" |
| Retry | send again after a failure | Claude "Retry" |
| Switch provider | a reply that failed because its provider is busy: the model chooser, then Regenerate on the model you pick | OpenRouter "Switch provider"; plain |
| Keep waiting | a reply that failed because its provider is busy: send it again and wait for the model until Stop | Windows "Keep waiting" (an app that does not respond) |
| Waiting for <model>. | the reply's status while Keep waiting waits for a busy provider | plain |
| Try again | the same, inside a sentence ("Try again later") | ChatGPT |
| Stop | stop the reply | ChatGPT, Claude |
| Send | send | everyone |
| Message (its name) | the composer's placeholder: its name in its chats, a sub-agent's name in that sub-agent's conversation | ChatGPT "Message ChatGPT", Slack "Message #channel" |
| Branch, Branch from here | a new chat from this message | ChatGPT "Branch in new chat" |
| Impersonate | it writes your next message as a draft | plain |
| Copy, Edit, Delete | on a message | everyone |
| New messages | the jump to the newest messages | Slack, Discord |
| Activity | the tool calls of one reply, from its "..." | ChatGPT (agent and deep research "Activity") |
| What (its name) saw | what one reply saw (its requests) | |
| Plan, Asks you, Waiting for you, Done, 2 of 5, done 19:00, since 19:00 | its plan, a button in the chat's head that opens it: its steps, a step where it asks you, it waits on your answer, every step done, the steps done, when a step was done, since when it is on one | Claude Code and Codex "Plan", ChatGPT agent "2 of 5 steps" |
| Daily at 04:00, Sundays at 03:00, Weekdays, Weekends, Every 2 hours from 10:00 to 22:00 | when a Routine's schedule runs, 24h times, from the server (schedules.words); a shape it cannot say stays the cron line | Google Calendar "Daily", "Weekly on Sunday" |
| Started by | a Routine run you or it started by hand, in Events | GitHub Actions "Triggered by" for the same job |
| (Its possessive pronoun) thoughts, Collapse thinking by default | its reasoning, and how its fold starts | Claude "Thinking", ChatGPT "Thought for" |
| Thinking | the line while it thinks | Claude, ChatGPT |
| Compacted | the conversation's older part was summarised | Claude Code "Compacted" |
| Attach | add a file to a message | ChatGPT, Claude |
| Reminders | messages it sends later in this conversation | Apple, Google "Reminders" |
| Model, Tools, Pages, Context | the tabs of Celine's side panel; Pages is also its pages' group in the Tools lists | ChatGPT "Model", "Tools"; Notion "Pages"; Claude Code "/context", LM Studio "Context" |
| Model | which model answers | everyone |
| load: | a note's head line, as the note writes it, in the Context tab's why ("load: off") | Obsidian properties |
| Load automatically, Notes that match each message | the Context tab's switch and its tooltip: each new message brings up the sections of its notes it matches | |
| Waiting for the GPU. | an image batch waits for its GPU (the lease) | ComfyUI, Stable Diffusion WebUI "Waiting" |
| Paused., The reply failed. | soft status words where the next line appears | |

### The agent

| Word | In Celine | Same word in |
|---|---|---|
| Name, Pronouns, Colour | who it is, on its Settings page | everyone |
| Set the agent's name, Set your name, Choose a model | the first-run steps, one at a time under its name; Choose a model also in the side panel while no chat model is set | Apple setup "Set Up", ChatGPT "Choose a model" |
| Persona | its description of itself | plain |
| Example lines | how it talks, a few lines of its own | plain |
| Banned words | the one list of words its voice leaves out | plain |
| Bias | how strongly the Banned words apply (a quiet default) | OpenAI "logit_bias" |
| Banned words and phrases work on tabbyAPI, llama-swap and llama-server models | the tooltip of the Banned words switch: the other kinds take no banned strings or word bias | plain tooltip, no explainer text (AGENT.md rule 7) |
| Use as portrait, Portrait set | a picture becomes its profile picture, and what it says once done | built on Copied |
| Ask Celine | its draft beside a long text | |
| Bastet | the guard who checks her actions before they run; a sender on the Intercom; the Guard setting | a name like Celine's |
| Created | when it was made; the sort option | Finder "Date Created" |

### Settings and models

| Word | In Celine | Same word in |
|---|---|---|
| General, Features, Guard, Appearance, Notifications, Models, Images & Music, Providers, Apps, Security, Vault, People, Tools, Email, Advanced, its name | Settings pages (Images & Music: ComfyUI and the image and music presets) | Claude "Appearance"; macOS; Bitwarden "Vault"; Firefox, Chrome "Advanced" |
| Turns at once, Tool rounds per turn, Turn time limit, Last answer time, Tool time limit, Job time limit, Messages per request, Log entries per chat, Backups kept, Attachments per message, Largest attachment, Largest text read inline, Largest message; seconds, messages, entries, days, bytes | the Advanced page: one number each, its unit beside it, saved on change | plain; LM Studio "Max concurrent", ChatGPT "attachments per message" |
| Login, Passkey, Password, PIN, This session | the Security list of how you log in, one row each, This session on the one this browser used | Apple, Google "Passkey", "Password", "PIN" |
| Sessions, Log out, Log out everywhere, Last seen, Unknown device | Security: every browser logged in, one row each named by the server ("Firefox on Linux", Unknown device without a user agent), This session on this one and Last seen on the rest; Log out on a row, Log out everywhere for every other one | GitHub "Sessions"; Google "Sign out of all", "Unknown device"; WhatsApp "last seen" |
| Confirm access, Use passkey, Confirm | the dialog adding a way to log in, Reset codes or Create token opens first unless you proved it in the last 5 minutes: your passkey, password or PIN | GitHub sudo mode "Confirm access", "Use passkey", "Confirm" |
| Source code | General: the link to the code this build runs, its commit on the link | GitHub "Source code"; the AGPL's offer of the source |
| Features | the Settings page of what it has, one switch each; also the onboarding's step | Windows "Optional features", Discord "Features" |
| Core, Base modules, User modules | the three headings of Features (Settings and Setup): what it is made of (on, no switch), what ships with it, the plugins you add | Drupal "Core", "Modules"; plain |
| Chat, Notebook, Mind, Plan, Intercom, Past conversations, Bug reports, Skills | the Core rows of Features, each with its one line | plain; as their places and skills |
| Next, Back, Skip | the onboarding's steps: on, back one, leave this one as it is | Apple setup, Android setup "Next", "Back", "Skip" |
| Step 4 of 7 | where the onboarding is, with its percentage and a bar | Shopify, Typeform "Step 2 of 5" |
| Decision model, Second look model | Bastet's two models on the Guard Setup step and Settings, Guard: the small one that decides (the Titles and descriptions role), the one that looks again | First look and Second look in Events |
| Address, Check | a service's address and the button that saves and tests it (its Setup step, Settings, Features, Edit; the Vault form too) | plain; Check as in Check provider |
| Connect a model | the Setup step that saves the first provider and picks its chat model | ChatGPT desktop "Connect", LM Studio "Connect to server" |
| Type, Auto-detect | a provider's kind, the first row of its form (Setup, Add a provider, a provider's edit dialog); Auto-detect, first and chosen at the start, lets the server say what answers at the address | Windows "Type"; printers, Wi-Fi "Auto-detect" |
| Pick the type if you know it. | the tail of the sentence after a Check with Auto-detect found no known kind | plain |
| http://192.168.1.20:11434, sk-..., Key, Home server | a provider form's placeholders: a value each, never "For example"; sk-... for a hosted kind's key, Key for a local one | everyone's forms |
| Key hash, Put this in the box's key.hash file | the read-only sha256 of the app's key for a box (the shellbox, the browser), with Copy, and its tooltip | GitHub "SHA256 fingerprint"; plain |
| Turns on once the shellbox answers. | the line under a feature switch that cannot be on yet, the service named (the server's `needs`); a click on the switch shows it as a toast | plain |
| Turns on once the shellbox is on., The shellbox is on., The shellbox is off. | the Shares switch: its `needs` while the Shellbox feature is off, and its probe's state line | plain |
| Turn on Shares?, Turn on | the confirm a feature with a warning (Shares) asks before its switch goes on (Settings, Features and the Features Setup step); off needs no confirm | macOS, Windows "Turn on" |
| The link of a share is its key. A page opens only for you while you are logged in, but anyone who gets a link can reach what it shares with a script until it ends (24 hours, or what you set) or you stop it. A public share opens for anyone. | the body of Turn on Shares? (the server's `warn`): the warning, the exception to "no explainer text" (AGENT.md rule 7) | plain |
| Shares | the feature of what it shares from its shellbox (Settings, Features, Setup) | Google Drive "Shared"; plain |
| Import, Importing, Done. 1,234 messages in the archive. | a Discord or WhatsApp Setup step once the account is linked: the first import, its line while it runs (the archive's message count, from the server) and its end; beside it the Messages switch | Google Photos, Apple Photos "Import" |
| Logging in with a user account may break Discord's terms; use it at your own risk. | under Log in with QR code (Settings, Discord and its Setup step) | plain |
| An embedding model lets Search find messages by topic, not only by their words, and a reranker model puts the best matches first. Any OpenAI-compatible server can run them, for example Ollama: run ollama pull nomic-embed-text on the server from Connect a model and it shows up here. You can also pick them later in Settings, Models, under Topic search and Reranking. | the Messages setup form (its Setup step, Settings, Features, Edit) above its Topic search and Reranking pickers: the one paragraph, the exception to "no explainer text" (AGENT.md rule 7) | plain |
| Same ComfyUI as Images | the Music Setup step and form once Images has a ComfyUI address: no second address field | plain |
| Her computer, Container, Own computer | the Setup step of where she works, her possessive pronoun in its title, once Shellbox or Her Mac is on in Features; Container and Own computer head each one's fields | plain |
| Her Mac, Run on the Mac, Waiting for the Mac., Connected: {about}., Set up again, Check | the Own computer form (the Her computer Setup step, its Settings page): the command with Copy and the waiting line until the Mac reports back, then Connected with the Mac's user, home, chip, memory, macOS and network as the script posted them; Set up again shows the command again; Check. What the Mac reports (its addresses, its token) is never a field to type | Apple "About This Mac", Bluetooth "Connected", Apple "Set Up Again" |
| Her Mac cannot reach localhost. Publish the app on your network (CELINE_PUBLISH=8766 in .env, then docker compose up -d) and enter its network address. | the Own computer form while the app's address is localhost, with the Address field under it | plain |
| Run the setup line on the Mac. | what Her Mac lacks until the Mac reports back: Features, All set, Check's lines, her settings | plain |
| Screen: 26 tools, Shell: Run the setup line on the Mac., Accessibility (Required): Not Granted. Grant via: System Settings > Privacy & Security > Accessibility | Check's lines on the Mac's form, one Dot each: each server's tool count or what kept it down, and each macOS permission the screen server lacks, as Peekaboo says it | plain; Peekaboo's own words |
| Anything worth knowing about you | the placeholder of About you | plain |
| All set, {name} is ready., On:, Off:, Off (did not answer), Not set up: {feature}. {why}, You can change all of this later in Settings. | the last Setup step: its title, then the features on, the ones switched on whose service did not answer, one line per one switched on that is not set up with what is missing, and the rest off, as muted lines, then that Settings changes all of it | Apple setup "All set", Google "All set"; plain |
| Guard mode, Watch, On | Settings, Guard: how Bastet acts while her feature is on (off is the feature switch) | Watch and On as in Events |
| Watch: every step runs at once, and Bastet writes down in Events what she would have done. On: Bastet checks a risky step before it runs, and stops it, freezes {her name} or asks you first, as her rules say. Off, the switch above: Bastet does not run at all. No checks, no notes, no looking back. | Settings, Guard, under Guard mode: one line per mode (guard/switch.py, calls.py) | plain |
| Decides quickly whether a risky step needs a check (it is also the Titles and descriptions model). Without one, Watch lets every step run, and On holds a risky step until one answers. | under the Decision model (Settings, Guard and the Guard Setup step): what it does and what Bastet does without it (guard/flow.py `_until`) | plain |
| A stronger model looks again at what the decision model stopped, before you are told: if it disagrees, you are not told and any freeze from it is lifted. With None, or when it does not answer, the first decision stands, and your note says why. | under the Second look model, the same places (guard/chain.py) | plain |
| No chat model yet. Bastet needs one to decide., Connect a model, Open Models | the Guard form when no saved provider offers a chat model: the line and the way to one (the Setup step in the onboarding, Settings, Models elsewhere) | plain, as Open Guard |
| {provider}: Connected, Not connected, Not checked, Not set up; Not on its provider any more. | the state under a picked Decision or Second look model: its provider's, as on Models, or that the model is gone | as on Models |
| Persona, Replies to {her name}, Notes to you | Settings, Guard: Bastet's three long texts (who she is, how she answers {her name} on the Intercom, how she writes her notes to you), each with Edit, History, Ask Celine and Use the default | Persona as on her page; plain |
| Make her warmer, Make her replies shorter, Make her notes plainer | what to ask Celine in the editor of Bastet's texts | as the hints of her own texts |
| Open Guard | the Features row's link to the Guard page, where Bastet is set up | plain |
| Open Images & Music | the Images and Music Features rows' link to their page while they are on | plain, as Open Guard |
| ComfyUI, Images, Music, On, Make default | Images & Music: the server's section, a section per kind, a preset's switch (it uses it), and the menu item that makes it the first of its role | ComfyUI's own name; macOS "Make Default" |
| Upload workflow, Replace workflow, Download workflow, Reset to defaults | a preset's workflow: a new preset from a ComfyUI workflow in API format, another workflow for one, the workflow with its placeholders as a file, an edited shipped preset back as it came | built on Upload and Download; ComfyUI "Workflow"; Reset to defaults as on Appearance |
| Name, Family, Good at, Prompting, Example, Variants, Variant, Size step, Largest image, Models, LoRAs, LoRAs per image, LoRA chain after, Sampler node, Trigger words node, Full model steps, Full model CFG, Full model sampler, Full model scheduler; pixels, megapixels | a preset's settings on Images & Music, each saved on change; the units beside them | ComfyUI, A1111 "Sampler", "Scheduler", "CFG"; Civitai "LoRA" |
| Reset codes | new recovery codes, the old ones stop working | Google "Get new codes" said plainly |
| API tokens | named tokens for scripts that call the API | GitHub "Personal access tokens" |
| You | the section of your name, time zone and about you (General, the onboarding) | Apple "You" in Contacts and Health |
| Network, Address | General: the section of the app's own address (logins, share links and her Mac's setup line use it) | macOS System Settings "Network"; Address as for a service |
| Behaviour | the section of her page for how the agent works (Ask before setting up services) | Discord, Firefox "Behaviour" sections |
| Show notifications, Replies, Intercom, Missions, Routines, Approvals | Notifications: the switch for all of them, one per kind (her replies in a chat, an Intercom message to you, a mission waiting for you, a failed run, a Vault or Bastet request) | Chrome "Show notifications"; Slack notification preferences |
| Push, Push to this device, This device, Remove | Notifications: this device's switch for pushes while no page of the app is open and focused, the list of every device that takes them (named by the server like Sessions), This device on this one, Remove on a row | Web Push; Chrome "Push notifications"; Security "This session" |
| Allowed, Blocked | the browser's notification permission (Notifications) | Chrome site settings "Allowed", "Blocked" |
| Blocked in this browser, Allow them in this site's settings in your browser., Notifications need https., This browser has no notifications. | Notifications: a permission revoked (the toast and the page), a page on plain http, a browser without them | plain |
| Accent colour, Text size | Appearance | macOS "Accent color", iOS "Text Size" |
| Colour, Background, Chat | the three sections of Appearance: the colours, the canvas behind everything, how the chat reads | Windows "Personalisation: Colours, Background"; plain |
| Tint, How much the accent colours the background and surfaces | Appearance, Colour: how strongly the accent hue colours the greys (0 % plain grey), and its tooltip | macOS "wallpaper tinting"; photo editors "Tint" |
| Contrast, The step in lightness between the background and surfaces | Appearance, Colour: how far apart the surfaces are in lightness, and its tooltip | macOS and Windows "Contrast" |
| Grain, Fine, Coarse, Paper, None, Opacity | Appearance, Background: the canvas's grain pattern (None: a plain canvas) and how much of it shows | Photoshop "Grain", "Opacity"; Procreate "Grain" |
| Reset to defaults, Reset the appearance? | Appearance: every choice back to the server's default, and its confirm | macOS, Windows, VS Code "Reset to defaults" |
| Rose, Peach, Sage, Teal, Sky, Violet, Miku | the colours: Accent colour (Appearance) and its Colour; Miku is a bright teal blue | macOS accent colour names; Miku after Hatsune Miku's hair |
| Loaded, Not loaded | model memory | Ollama "loaded" |
| Context size, Server: {n}, differs from the server | a model's written context and what its server says it runs with (the server's number counts) | LM Studio "Context Length"; Open WebUI "Context Length"; plain |
| Unload after | how long an idle model stays loaded | plain; Ollama says `keep_alive`, Open WebUI "Keep Alive" |
| Read-only, Can edit | sub-agent access | Google Docs "Can edit" |
| Device | where a drone runs: one of her devices (mac), next to Agent CLI on a sub-agent | Apple, Google "Device" |
| Instructions | a mission's task text | ChatGPT and Claude Projects "Instructions" |
| Minimum time, Runs at least until | how long a mission runs at least before it may close as done, in its form and on its page | plain |
| Result, Results | what a mission or sub-agent sends back | Apple Shortcuts "Show Result", Google "Results" |
| Trigger, Triggers | what starts a run (Watch, Event) | Zapier, GitHub Actions |
| Schedule, Runs after, Reviewer, Turns | Routines | GitHub Actions "schedule" |
| No runs | a day in Routines with nothing in it | GitHub Actions "no workflow runs" |
| Cron | only the label of a cron expression field | cron, Vercel "Cron Jobs" |
| Take control | you take its browser | OpenAI Operator "Take control" |
| Return control | you give its browser back | plain; Operator names only "Take control" |
| Extract text | read a document's text again | |
| Running, Waiting for you, Failed, Done | states | GitHub Actions, everyone |
| Invalid | a citation in its reply that failed the check, shown with its id | everyone |
| Dismiss, Dismiss all | an Intercom thread leaves the waiting list | Android notifications |
| Continue in a chat | an Intercom thread goes on as a new chat with it, its messages carried over | plain |
| Go to | the Ctrl+K switcher | VS Code "Go to File" |
| Guides | its skill guides in the Notebook | |
| Save, Cancel, Close, Add | | everyone |
| Shared from (its possessive pronoun) box | the Pages tab's heading over what it shares from its shellbox (a server it runs, a file or folder of its home), and a share card's line until its state is known | plain; Google Drive "Shared with me" |
| Running, Not running, Ended | a share's state by its dot: its server answers, nothing answers, it ended (its time ran out, it or you ended it, or its box was reset) | Docker Desktop "Running", "Not running"; plain |
| 3h 12m left | a running share's time until it ends, beside its state | Google Meet "2 min left"; plain |
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
| Past conversations | your earlier conversations with it | ChatGPT "Reference chat history" for the same job |
| Terminals | its terminals in its shellbox | the Terminals place |
| Code | its coding agent in its shellbox | VS Code, Cursor "Agent" |
| Intercom | its messages and threads | the Intercom place |
| Missions | the missions it works on | the Missions place |
| Plan | its plan, from the button in the chat's head | as above |
| Routines | its schedules and their runs | the Routines place |
| Web | it searches the web and reads pages | ChatGPT, Claude "Web search" |
| Email | its inbox | everyone |
| Browser | its browser | the Browser place |
| Vault | its passwords | Bitwarden "Vault" |
| Pages | its pages | as above |
| Image generation, Music generation | it makes images and tracks with ComfyUI | Midjourney, Suno |
| Models | the models it may ask | as above |
| Writing | it writes a post or a message in its own voice | Grammarly, Notion AI "Writing" |
| Skills | it loads a skill's tools and reads its guide | Claude "Skills" |
| Bugs | the problems with the app it files for you | as in Events |
| Portrait | its profile picture | as in "Use as portrait" |
| Voice notes | its voice notes to you, spoken by the Read aloud model | WhatsApp, Telegram "voice message" for the same job |
| Persona | its description of itself | as above |
| Settings | it reads every setting and proposes changes you approve | the Settings place |
| Mood and thoughts, Mood, Status, Thoughts | its mood, the emojis by its name, the thoughts it writes down | Slack "Set a status"; Daylio "Mood" |
| Sub-agents | the helpers it gives instructions to; also the side panel's tab with every sub-agent of the chain, each under the one that started it | Claude Code "Sub-agents" |
| Cloud model | the approval card of a sub-agent that would run on a model of a cloud provider (OpenRouter, Anthropic): its name, the model and the start of its brief | OpenRouter, Anthropic: cloud providers |
| Discord, WhatsApp | read live from your accounts | their own names |

### Events

The words of the Events place (`server/celine/events.py` gives them).

| Word | In Celine | Same word in |
|---|---|---|
| Guard, Celine, Background tasks | the tabs: what Bastet decided, what she did in her chats, what ran without you (runs, tasks, missions, sub-agents, jobs); Guard is also the Intercom tab of Bastet's threads | Guard as the setting; Background as in the transcript |
| Turn, Turns, Tools, Notebook, Background jobs, Intercom, Runs, Freeze, Pass, Titles and moods, Persona, Bugs | the Type filter, and a turn's row; Persona: her own edits of her persona and Example lines | Turns as in Routines; Tools as in the side panel; GitHub Actions "workflow runs"; Intercom as the place; Freeze and Pass as Bastet says them |
| Title, Mood, Document descriptions | the items of Titles and moods: a conversation's title, its mood after a turn, the documents' descriptions | plain |
| Bastet | the guard's decisions (her name) | |
| Wrote, Edited, Deleted, Moved ... to ... | its notebook writes ("Wrote people/noor.md") | Google Docs activity "edited", "moved" |
| To (the user's name) | a message it sent you | Gmail "To" |
| Stopped, Skipped, Cancelled, Scheduled, Sent | states besides Running, Done, Failed and Paused. | GitHub Actions "Skipped", "Cancelled"; Gmail "Scheduled", "Sent" |
| Allowed, Stopped, Frozen, Found, Approved, Denied, No answer, Pass | Bastet's decision on a call, the words of her banners | Android permissions "Allowed", "Denied" |
| Would have stopped, Would have frozen | what Bastet would have done while she only watches, as her banners say it | |
| Freeze, Unfreeze, Pass on (a rule), Queued | a freeze and its end, a pass you gave, a work item that waits | the words above; Queued as in the transcript |
| Could not decide | a decision Bastet could not make (her model gave no answer: it was down, timed out or the call did not fit), counted apart from Failed | |
| Decision, Rule, Mode, On, Watch | Bastet's filters: what she decided, by which rule, and whether she acted (On) or only watched (Watch) | GitHub code scanning "Rule"; Watch as in Bastet's setting |
| Details | open one decision of Bastet's from its row | macOS Finder, Windows "Details" |
| First look, Second look, Yes, No | one decision's chain: the small model's verdict and why, then the big model's second look (guard/chain.py) | a second opinion, as people say it |
| Question, Score, Model, Time, Source, Quote, Piece, Arguments | one decision's facts: what its small model was asked, the score it gave, which model, how long, where the text came from, what it quoted, the private piece, the call's arguments | OpenAI evals "score"; Chrome DevTools "Time" |
| Allow once, Pass | answer a decision from its row as her banner does: the same call runs once; a pass on its rule in that chat | Chrome site permissions "Allow this time"; Pass as Bastet says it |
| Open, Closed, Close, High, Medium, Low | a bug it filed: open until you close it (Close on its Intercom thread); its severity, beside Open in its row ("Open, high") | GitHub Issues "Open", "Closed", "Close issue"; Jira priority "High", "Medium", "Low" |
| No events yet, No events match | the empty list, and a list the filters empty | plain |

### Actions

| Word | In Celine | Same word in |
|---|---|---|
| Add image, Add key, Add model, Add models, Add passkey, Add password, Add PIN | add one of these | built on Add; Bitwarden "Add passkey", LM Studio "Add model"; Windows "Add PIN" |
| Allow notifications, Not now | the browser's notification ask, and saying no to it | Slack, Chrome "Not now" |
| Apply | use the picked settings or filter | everyone |
| Change, Change model | pick another value or model | ChatGPT "Change model" |
| Default, <model> | the model chooser's first row, and the Model row while none is picked: the model a conversation (or a mission) comes to without its own | ChatGPT "Default" model; macOS "Default" |
| Check and add, Check email, Check provider | test a connection (and add it when it works) | Thunderbird "Check mail", Open WebUI "Verify connection" |
| Check again | ask a feature's probe anew (the Features page, the onboarding); read the browser's notification permission anew (Notifications) | Chrome "Check again" (connectivity), macOS Software Update "Check Again" |
| Choose channels, Choose chats | pick what the connection reads (WhatsApp and Discord chats: their apps' word stays) | WhatsApp "Choose chats" |
| Clear recent searches | empty the recent searches | Google, YouTube |
| Close tab, Close terminal | close one of its tabs or terminals | Chrome "Close tab", VS Code "Close terminal" |
| Copy link, Copy message link, Copy prompt | copy one of these | Slack "Copy link", Discord "Copy Message Link" |
| Custom range | a date range you pick | Google Analytics, Gmail |
| Approve, Deny | say yes or no to a request (the Vault, a gate, Bastet's banner of an ask, a change of Settings) | macOS, Android permissions |
| Settings, Change {target}., Default, Empty, On, Off, Saved, Not set, New value, Removed | her request to change Settings in the chat: the kind's name, its line, then each field's before and after (a secret by its state only) | macOS "Change Settings"; Bitwarden "Saved" |
| Edit prompt | change a picture's prompt | Midjourney, Leonardo |
| Freeze, Unfreeze, Frozen | stop everything she does until you let her go on (the guard, or you by hand); Frozen in the rail and Unfreeze on Bastet's banner | Revolut, Monzo "Freeze card" |
| Go to the original message | jump to the message a reply or result came from | Slack, Discord "Jump to message" |
| Lock, Unlock | the Vault | Bitwarden "Lock", "Unlock" |
| Log in with QR code | link WhatsApp or Discord with the phone | Discord "Log in with QR Code" |
| Mark as read, Mark as unread, Mark all as read | notifications and messages | Gmail, Slack |
| Move up, Move down | the order of a list (models) | Google Docs, Word |
| Move ended terminals to Trash | the ended terminals in one go | built on Move to Trash |
| Ended today, Ended yesterday, Ended 12 Mar | the heads that group ended terminals by day | Slack "Today", "Yesterday"; Gmail |
| Open, Act, Scroll, Read, Close | the names of its browser tools, in the History's filter and the manual call (Read is what it did when it looked at the page) | automation "open", "click", "scroll"; browser "Reader" |
| 1 screenshot, 3 screenshots | the pictures a browser call took | macOS, Windows "Screenshot" |
| Conversation | the title of a mission's or a sub-agent's conversation page (the Open conversation link opens it) | ChatGPT, Claude: the chat is a conversation |
| New conversation, New recovery codes, New token | the New X of these | Claude "New conversation", GitHub "Generate new token" |
| Open Browser, Open Gallery, Open in Studio, Open mission, Open Notebook, Open Studio | a link that names where it goes | VS Code "Open Settings" |
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
"Again" fails while "Try again" passes; `word` bans the word or phrase anywhere in a UI
string as a whole word (any case), so "Filter conversations" fails while "Filters" passes.

| Banned | Match | Use instead | Why |
|---|---|---|---|
| Again | label | Regenerate | ChatGPT, Claude |
| Write it again | label | Regenerate | same job |
| Run again | label | Run now | plain |
| What-if, what-ifs, What-if from here | word | Branch, branches, Branch from here | ChatGPT |
| Filter | word | Search | plain |
| How hard, Strength | label | Bias, as a quiet default of Banned words | Bias |
| Never use, Avoid, Wording, Words it never uses | label | Banned words | one list, one clear thing |
| Lately | label | Recent | ChatGPT, Claude |
| Tweak | word | Edit | everyone |
| Shipped list | word | Defaults | everyone |
| Made | label | Created | Finder |
| Start values | word | Defaults | plain |
| Brief, Briefs | word | Instructions | ChatGPT and Claude Projects |
| Report, Reports | word | Result, Results | plain |
| Started by events | word | Triggers | Zapier, GitHub Actions |
| Working | label | Running | one word for the state; GitHub Actions |
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

Runtime strings the lint cannot see whole, fixed by hand: "Compacted. Click here to view new Context" becomes "Compacted" with an Open link.

## The lint block

One entry per line: `label|word <banned> => <replacement>`. Lines starting with `#` are
comments. Lines are checked in order; the first match is reported. Matching ignores case.

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
word Needs you => Waiting for you
label Try => Attempt
label Tries => Attempts
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
```
