# Changelog

What each release changed for the people who run it, newest first. The images carry
the release as their tag (`CELINE_TAG=v1.2`, docs/DEPLOY.md "CI").

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
