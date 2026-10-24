# Your shellbox

Debian 13, yours: install, build, break and fix what you like. You are `celine`; `sudo`
needs no password.

This file is part of the box: read-only, and every reset puts it back.

## What lasts

| Where | Lasts |
|---|---|
| your home, /home/celine | always |
| everything else: apt installs, /etc, /opt, your login shell, this file | until the next reset |
| /tmp | until the next restart |

Anything that must survive goes in your home: your files, your dotfiles (~/.bashrc,
~/.config) and what pip and `npm install -g` put in ~/.local. Notes about what you did and
learned go in your notebook, with the notebook tools, not in a file here.

## The reset

Every morning the box goes back to a fresh image. Only your home stays; running
programs and open terminals end.

- `sudo reboot` restarts the box. A restart is not a reset: everything stays.

## Your commands

Every command shell__run runs leaves its files in /var/lib/shellbox/cmd/: `<id>.start`
(when, and the command), `<id>.log` (its output, gzipped an hour after it ended) and
`<id>.exit` (the exit status and the seconds it took). They last until the next reset.
The run function, `__shellbox_run`, comes from /etc/shellbox/run.sh, which
/etc/bash.bashrc reads: add to PROMPT_COMMAND rather than replace it, and leave PS0 alone.

## Agents

opencode comes with the box: `agent list` names it and your models, `agent opencode
"task"` runs it on a task in the current folder, and `opencode` runs it by hand. It
reaches your models through the app with $OPENAI_BASE_URL and $OPENAI_API_KEY.

Your coding agent (the `code` tools) is opencode's server on 127.0.0.1:4096, started at
every boot as you, its output in /var/lib/shellbox/opencode.log. Its password is
~/.config/opencode/server.password. Its config, ~/.config/opencode/opencode.json, is
written at every boot and whenever your models change; your own settings go in
~/.config/opencode/opencode.jsonc, which wins and which the box never writes.

REA (`rea --help`) takes apart apps and binaries you have no source for; opencode has it
as a tool, and native binaries need an engine you install (Ghidra, JADX).

## Skills

/opt/skills holds skills that come with the box. /opt/skills/anatomy makes isometric
SVG figures (Node and Chromium are installed); its SKILL.md says how. Copy an example
into your home before you build on it: /opt is gone after the reset.

## Installing and changing the system

- Install anything you want. You never have to worry about wasting resources.
- The box updates its package lists at every start, before ~/boot.sh runs:
  `sudo apt-get install -y <package>` works right away.
- Installs and changes outside your home are gone after the reset. To have them back
  every morning, put the commands in ~/boot.sh: it runs on every start of the box, as you,
  with sudo, without a terminal (so `-y` and `DEBIAN_FRONTEND=noninteractive`), for at
  most 15 minutes, output in ~/boot.log. Every line must be safe to run twice.
- The same goes for a login shell (`sudo chsh -s /usr/bin/zsh celine`), a service or a
  config in /etc: the command goes in ~/boot.sh.

## Limits

- Your home has a fixed size: the greeting shows how full it is, `du -sh ~` what it
  holds. Past it, your next result says so; when writing fails with "No space left on
  device", the same: delete what you do not need, or ask for more room.
- 4 GB of memory, /tmp included: past it, the biggest program is killed.
- 2 CPUs: more work only runs slower.
- 1024 processes: past it, new ones fail to start.
