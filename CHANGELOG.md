# Changelog

What each release changed for the people who run it, newest first. The images carry
the release as their tag (`CELINE_TAG=v1.2`, docs/DEPLOY.md "CI").

## v1.2

### Added

- Missions: pick the model when starting one; the model switch is in the conversation head.
- Coding sessions can be watched live in a terminal; web search for the coding agent is an opt-in setting.
- Files she sends are cards under her reply.
- The vault shows a banner while it is locked, dismissed for a day.
- Advanced: a time limit for routines (0: none) and `requests_keep`, the days a model request is kept (0, the default: forever).
- The database files are copied beside themselves before a migration changes them; the last three copies are kept.
- The shellbox's morning snapshots go after `SHELLBOX_SNAPSHOT_KEEP_DAYS` (default 7).
- The shellbox home has a soft and a hard cap: past the soft one she is told, past the hard one no upload lands.
- A request forwarded by a proxy that is not trusted is logged once as a warning.
- GitHub runs the tests, ruff, eslint and the web build on every push and pull request; images are also tagged with the git tag.

### Changed

- Wrong PINs from any address pause PIN login for everyone.
- The data dir is mode 700 and what the app writes there is its user's alone.
- The app image type checks the web UI before it builds it.
- The web UI reloads after an update or a lapsed proxy login instead of hanging on reconnect.
- A section that fails to load shows Retry instead of a blank app.
- A streamed reply re-renders only the message that changed.
- The terminal reads its colours from the theme.
- opencode 1.19.4 in the shellbox; it picks up models added after the box started.

### Removed

- The WhatsApp chat bridge: she no longer answers or writes in your "Message yourself" chat, and approvals, freezes and Bastet's notes reach you in the app only. Bridged conversations become ordinary chats. The Discord and WhatsApp archives stay.

### Fixed

- A missing model or a rejected key moves on to the next model; a chat whose model is gone says which.
- A reply cut by the context window counts as cut for length (Anthropic).
- A store error fails only its turn; end hooks cut off by a shutdown run at the next start.
- A stopped sub-agent ends its mission's wait; a reply from the user ends needs you.
- A time limit stops a call still waiting for its slot.
- An upload stays with the conversation it started in.
- A scoped document search narrows before the result cap; notebook paths match exactly.
- A 2am routine runs once when the clocks go back; a note a run wrote itself is kept.
- Private shares open by the browser box's address.
- The vault syncs on its first unlock, also right after a boot.
- Health answers 503 once the store's writer stopped.

<!-- the rest of this release's changes are added at merge -->

## v1.0.0

The first release.
