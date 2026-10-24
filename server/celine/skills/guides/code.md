---
name: code
description: Your coding agent in your shellbox. Hand it a coding task in a folder of
  yours with a model you pick; it reads, edits and runs the code on its own in the
  background and answers when it is done.
words: coding agent, code session, refactor, implement, fix the tests
---
Your coding agent is opencode, running in your shellbox, and it works on a folder of
your home the way a colleague would: it reads the code, edits files, runs commands and
tests, and tells you what it did. It runs in the background, so you keep talking and
poll it. You drive it with these five tools; the box holds its password, you never need
it.

## When

- A coding task with more than two or three edits, or one where you would be typing
  commands into a terminal and reading output for a while: use the agent and poll.
- One command, one look, one small edit: a terminal (shell__run) is quicker.
- Work for someone else to carry while you do other things: a sub-agent with `cli`
  (the sub_agents guide). The coding agent is for work you drive yourself.

## Sessions

A session is the agent's memory of one folder and what it did there. Reuse the id:
`code__start(dir, task, model, session_id=...)` carries on with everything it learned
and changed, so follow-ups ("now add tests for that", "the build fails, fix it") cost a
fraction of a fresh start. Open a new session for a new folder or an unrelated job.
`code__list` shows your sessions when you lost an id.

## The model

Pick the model per task: `models__list` names the models you may use and their refs,
and the ref is what you pass as `model` (such as `openrouter/provider/model`).
A strong cloud model through the proxy is fine for hard work: a tangled bug, a design
change, a big refactor. A small local model does for a rename or a one-line fix. An
unknown ref is refused with the list.

## How it runs

- opencode runs as a server in your box (`opencode serve` on 127.0.0.1:4096, as you),
  started at every boot. The box starts it again a few seconds after it exits.
- Its output goes to /var/lib/shellbox/opencode.log: `tail -50
  /var/lib/shellbox/opencode.log` shows why it failed or restarted.
- Its config, ~/.config/opencode/opencode.json, is written by the box at every boot and
  whenever your models change: one provider, `celine`, with every model of yours and its
  real limits (the context and the most one answer may take, as the app knows them),
  every permission allowed, and REA as an MCP server (the shell guide). Do not edit it:
  the next write replaces it.
- Your own settings go in ~/.config/opencode/opencode.jsonc, in opencode's format. They
  win over opencode.json and the box never writes that file. For example a model's
  limits as `provider.celine.models.<ref>.limit` (`context`, `output`), `permission`,
  another MCP server under `mcp`, or `model` for opencode by hand (code__start uses the
  model you pass).
- A change counts once opencode starts again: `pkill -f "opencode serve"` while no
  session runs, and the box starts it again within seconds, on your settings.
- You may run opencode by hand too, in a terminal: `opencode` there (through
  shell__type), or `opencode run -m celine/<ref> "task"`, or `agent opencode` (the shell
  guide). It reads the same config.

## The tools

- **code__start(dir, task, model, session_id=None)** hands the task over. `dir` is a
  folder in your home that exists ("projects/site", "~/tools"). Write the task as a
  brief: the goal, what done looks like, what to run to check it. It answers with the
  session id and "running".
- **code__status(session_id)** says running, done or failed (and why). Poll with this,
  not with code__result: it is cheap. Wait about {{poll}} s between two looks; a real task
  takes minutes, so do something else in between, or tell {{username}} you will come back.
- **code__result(session_id)** brings its last answer (the first {{chars}} characters),
  "Files changed: ..." from its diff when it changed files (or why it could not read
  the diff), and "Tests: ..." when its
  output showed a pytest or npm summary. Read the files yourself with shell__run when the
  answer is not enough.
- **code__abort(session_id)** stops a running session. What it changed so far stays in
  the folder.
- **code__list()** lists your sessions, newest first.

## When it fails

Every failure says what opencode or the box said, and what to do. Act on it:

- "opencode answered <status>: <its words>": opencode refused or broke; its words say
  why (a bad setting in your opencode.jsonc, a model it does not know). Fix that, or read
  its log.
- "opencode is not answering ...": the server is down. When it says it exited, the box
  is starting it again: try in a minute. When it stays down, read its log; a broken
  opencode.jsonc is the usual cause.
- "failed" with opencode's error (such as `APIError: ...`): the model or the app's model
  proxy said no; the error names it.
- "failed" with "No answer came": the model never answered: check the ref with
  models__list, and that the app's models are running.
- "has no models yet": the box has not reached the app's model proxy since it
  started; it keeps trying.
- "The coding agent (opencode) did not start in the box": `sudo reboot` in a terminal
  restarts the box; tell {{username}} when it still fails.

## Budgets

- One session at a time per folder: a second start on a busy session is refused; wait
  or abort.
- Every call reaches the shellbox at its address with the app's key. When the box does
  not answer, the result says so and names the address; tell {{username}}.
- Its answer is cut at {{chars}} characters; a brief that asks for a short final answer
  (what changed, what ran) gets you the whole of it.
