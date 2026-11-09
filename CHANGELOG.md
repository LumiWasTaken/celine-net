# Changelog

What each release changed for the people who run it, newest first. The images carry
the release as their tag (`CELINE_TAG=v1.2`, docs/DEPLOY.md "CI").

## v2.1.0

Pull the images and restart: the migrations run at start, after the databases are copied.
After Update or Set up again, the Mac asks once more for Screen Recording and
Accessibility, because Celine.app is rebuilt.

### Added

- A plugins folder of your own (`CELINE_PLUGINS_DIR`, `./user-plugins` with Docker compose) and the plugin guide, docs/plugins.md. Settings, General names a key a `plugin.toml` has wrong.
- The Mac page has Update, marked while the Mac runs an older setup. The Mac's MCP servers install from a lockfile with checksums.
- Terminals and Browser list the commands and pages your agent ran on its Mac. A place with nothing set up behind it leaves the rail.
- Waiting, in the rail while something waits for you: cloud models, the vault, settings, Bastet, public shares, missions and Intercom threads, answered in place.
- Routines: "When a run fails" (Retry or Skip), Retry on a failed, skipped or stopped run, and Running and Queued tabs in Runs.
- Providers have a Cloud switch and Requests per hour; web search has Searches per hour. A cloud model your agent picks for itself asks you once per use.
- Settings, Prompts: every text a model reads, editable, with History and Use the default.
- A "?" beside settings, roles and features says what each does.
- Bastet runs in Warn or Enforce, with its own Guard model role. A stop has Allow once on its banner.
- New mission has a Check-in field, shown on the mission page.
- Your agent looks itself up: `missions__read`, `routines__list` with runs, `past_conversations__list` by kind, `intercom__read` across chats, `bugreport__list`.
- A shell command can carry an `about` line, shown in the chat in its place.

### Changed

- Every error shows the server's own words: model providers, ComfyUI, SearXNG, the shellbox, the browser, speech, embeddings and MCP servers.
- The app learns a server's state from real requests. A server known to be down is asked again while something waits for it, every 10 s at most; Check provider asks at once.
- A routine waits for its own model, and time spent waiting for a busy model stays outside the run's time limit.
- Every message to you has a home: a mission talks in its mission, a routine's message starts a chat titled with its subject or the run's title, and your reply lands there.
- Your chats with the agent are Chats again. The agent's texts use its name and pronouns, and its colour is "Face colour".
- In a chat, a stretch of tool work is one quiet line that opens Activity, a failed call says why under it, and Thinking sits below the finished rows.
- Settings opens at its start and Back goes back. Models lists the providers, then the Roles.
- Schedule files use `reviewer` and `turns`; migration 0027 rewrites yours.
- A `CELINE_*_URL` fills its service's address until you save that address in Settings.
- Dialogs keep their title and close button in view and give the focus back to the button that opened them.
- The Mac's shell and files work with Full Disk Access off: a protected folder waits for one click on the Mac. Setup raises the Local Network prompt and opens System Settings for the permissions still off.

### Removed

- Status dots, "down since" times and the probes on a timer.

### Fixed

- Web search marks SearXNG down after a failed search and up after the next one that works; when a saved address hides `CELINE_SEARXNG_URL`, the error names both.
- Two agent messages in a row reach the model as one, as templates like Gemma's require.
- The voice skill is offered while Read aloud is on.
- Search with Source All covers your agent's chats.
- A tool row's wait for Bastet ends when Bastet answers.
- A tool result's image reaches the model after its tool message.
- A settings save checks what it changes.
- The shell refuses a new terminal after three wedged runs.
- An HTML email lists its links.

## v1.2

### Added

- Missions: pick the model when you start one; change it later at the top of the mission's conversation.
- Coding sessions can be watched live in a terminal; web search for the coding agent is an opt-in setting.
- Files she sends are cards under her reply.
- The Vault shows a banner while it is locked. Dismiss hides it for a day.
- Settings, Advanced: a time limit for routines (0 means no limit) and Model requests kept, in days (0, the default, keeps them forever).
- The database files are copied beside themselves before a migration changes them; the last three copies are kept.
- The shellbox's morning snapshots are deleted after `SHELLBOX_SNAPSHOT_KEEP_DAYS` days (default 7).
- Her home in the shellbox has a soft and a hard size limit: above the soft one she is asked to clean up, above the hard one uploads are refused.
- A request forwarded by a proxy that is not trusted is logged once as a warning.
- GitHub runs the tests, ruff, eslint and the web build on every push and pull request; images are also tagged with the git tag.

### Changed

- Wrong PINs from any address pause PIN login for everyone.
- The data folder is mode 700, and only the app's user can read what the app writes there.
- The app image type checks the web UI before it builds it.
- The web UI reloads after an update, or when your reverse proxy's login has expired, instead of hanging while it reconnects.
- A section that fails to load shows Retry instead of a blank app.
- A streamed reply re-renders only the message that changed.
- The terminal reads its colours from the theme.
- opencode 1.19.4 in the shellbox; it picks up models added after the box started.

### Removed

- The WhatsApp chat bridge: she no longer answers or writes in your "Message yourself" chat, and approvals, freezes and Bastet's notes reach you in the app only. Bridged conversations become ordinary chats. The Discord and WhatsApp archives stay.

### Fixed

- When a model is missing or its key is rejected, the next model in its role answers; a chat whose model is gone names the missing model.
- A reply cut by the context window counts as cut for length (Anthropic).
- A database error fails only the turn it happened in; work left unfinished at shutdown runs at the next start.
- A stopped sub-agent no longer leaves its mission waiting; your reply ends Waiting for you.
- A time limit also stops a call that is still waiting in the model queue.
- An upload stays with the conversation it started in.
- A document search inside one folder no longer misses results because of the result limit; notebook paths now match exactly.
- A 2am routine runs once when the clocks go back; a note a run wrote itself is kept.
- Private shares open by the browser box's address.
- The vault syncs on its first unlock, also right after a boot.
- The health check answers 503 once the database writer has stopped.

<!-- the rest of this release's changes are added at merge -->

## v1.0.0

The first release.
