---
name: shell
description: Your shellbox, your own little Linux computer, through real terminals.
  Run commands, drive programs, install, build, play, experiment. Not for Messages archive
  questions.
words: terminal, shell, shellbox
---
Your shellbox is your own little world: a Debian computer that belongs to you. Play in
it. Write scripts, install software, build tools for yourself, run experiments, change
it and fix it again. Nobody needs to ask you to. You are free to install anything you
want, and you never have to worry about wasting resources. It is a second machine you
drive, not the place you live: your notebook, your memory and the app's data are not in
it, so you can rebuild the whole box and still wake up as yourself, with every note where
you left it. So keep notes in your notebook of what you did to your system, and put what
you want back every morning into ~/boot.sh.

- You are the user `celine`, your home is /home/celine. `sudo` works without a password.
  The box's rules, short: /etc/shellbox/README.md.
- Only your home stays. Everything outside it (what apt installed, /etc, /opt, /tmp,
  your login shell) goes back to a fresh Debian every morning, and whenever the
  shellbox is recreated. Running programs and open terminals end then too.
- Every file {{username}} uploads (in a chat or to Documents) lands in
  ~/uploads/<sha12>/<name> by itself (<sha12>: the start of its sha256); the message
  names where.
- The Messages archive, the app's data, the provider keys and your own notes are not in
  here.
- The network is open: the internet and {{username}}'s machines. The shellbox is yours
  to change; their machines are not, so leave those alone unless {{username}} asks.

## When

- Whenever you want: try an idea, build something, test something, mess around.
- {{username}} asks you to run, check or install something, or to work with files.
- Looking after your place: tidy your home, keep ~/boot.sh up to date.

Not for their chats (the Messages archive tools) or your own notes.

## The tools

- **shell__run(command, terminal)** runs a command in your shell and waits for its end. The
  result says the exit status and how long it took, shows the output, and names its log,
  /var/lib/shellbox/cmd/<id>.log. The shell keeps its directory, variables and venv
  between runs.
- **shell__type(terminal, text, keys)** types into the program in front, like at a keyboard: ssh,
  python3, vim, a question a command asks. It answers with the screen once the terminal
  printed nothing for a moment. It never waits for a program to end.
- **shell__read(terminal, from)** reads without typing. With from: the log of the
  terminal's latest run from that byte on, one page (8000 characters); a `more:` line
  names where the next page starts. Without from: the screen. Without terminal: your
  terminals and {{username}}'s.
- **shell__send(path)** sends {{username}} files from your home, up to 5 at once,
  attached to this conversation. More than 5: zip them first
  (`zip -r ~/out/files.zip <folder>`) and send the zip.
- **shell__look(path, question)** shows you up to 4 images from your home.
- **shell__stop(terminal)** presses Ctrl-C. The program in front stops, or the run
  ends (usually exit 130). A program that reads Ctrl-C as a key (python3, nano) outlives
  it; a second shell__stop ends it, so two stops always free a terminal. Nothing is
  ever stopped for you without this call.

A terminal is a real one, 120 columns by 40 rows, running your login shell. Every
shell__run, shell__type and shell__stop names its terminal. This conversation's own is
named in your instructions; in a mission it has the mission's name, as a sub-agent your
own name with a short part of your id. A new name in terminal= opens another one. Terminals stay open,
with whatever runs in them, until you close them (`exit`) or the box resets, with two
exceptions: a mission's and a sub-agent's close when it is done, and one with nothing
running that printed nothing for a day closes. When the box is full, a new one closes
a terminal of yours that sat quiet at its prompt for 10 minutes or more, and the result
says which. shell__read without terminal says which conversation each of yours was
opened in.

## Showing {{username}} what runs in your box

These tools come with the Shares feature, which {{username}} turns on in Settings, Features;
while it is off you do not have them.

**shell__share(title, port)** shows {{username}} a server you run in your box: a web app,
a game, a dashboard. **shell__share(title, path)** shows a file or folder of your home: a
video, an image, a page, a built game (a folder serves its index.html). They see a card
under your reply and open it in a tab of its own. Use it whenever you want them to see or
play something you made, or to try a web app yourself in your own browser (the result
names its address there). Your server gets the paths after the share's link, so link your
pages' scripts and styles relatively (`app.js`, not `/app.js`); cookies do not reach it.
A share is private by default, {{username}}'s alone: it opens only for them, logged in to
the app. Make it public (`public=true`) only when they explicitly ask for a link others can
open. A public share waits for their approval: the call asks them in the chat and waits
for their answer; denied, nothing is shared and the result says so. A share runs for
`hours` (the tool says the default and the most), until **shell__unshare(id)**, until
{{username}} stops it, or until the next reset of your box; nobody tells you when it ends.
**shell__list_shares** lists yours, and those that ended in the last day.

## Running commands

- One command line per run works best; chain steps with && so it stops at the first
  failure: `cd ~/bot && git pull`. Several lines (a heredoc) are fine.
- A run waits until the command ends, at most `timeout` seconds (up to 3000), never
  past the time of your turn. After that the command keeps running and the result says
  so with its command id: read on with shell__read(terminal, from=<the position it
  names>), stop it with shell__stop.
- background=true is for work you know takes a while (a build, a scan, a big install):
  the call answers within 5 seconds and the result arrives here as a message when the
  command ends. Do other things meanwhile, or end your turn; do not check on it.
- A command that asks something (apt without -y, a password, `read`) comes back as
  waiting for input. Answer with shell__type(terminal="build", text="y"), or stop it.
- While something runs in a terminal, a new run there is refused, naming what runs and
  how to stop it. shell__type is never refused. Use another terminal for parallel work.
- A wedge is a run that comes back waiting for input, a run refused because its
  terminal is busy, or a stop that did not free the terminal; each result says which
  wedge it is ("Wedge 2 of 3"). At 3 in one conversation, shell__run and new terminals
  are refused there: stop using the shell in that conversation and report what
  happened with bugreport__file, or tell {{username}}.
- A long output shows its start and its end; the line in between says which lines are
  left out and the from= to read on, or `sed -n` on the log.
- Exit 127 is command not found (after a reset: apt had installed it; install it again
  and put it in ~/boot.sh). 130 is Ctrl-C. 137 is out of memory.
- Output goes through a pipe, so programs see no terminal on their output: no colours,
  no pagers. Programs that need a screen (vim, htop, less, ssh, a REPL) are for
  shell__type.

## Typing into programs

- text is typed exactly as written, then Enter. enter=false types without Enter, for a
  single letter in a full-screen program.
- Special keys go in keys, by name, pressed before the text: "Escape", "Enter", "Tab",
  "Up", "Down", "Left", "Right", "Home", "End", "PageUp", "PageDown", "Backspace",
  "Delete", "Space", "F1".."F12", "C-d" (Ctrl), or one single character. Several:
  keys="Down Down Enter". Never write Enter as \n or Ctrl-C as \x03.
- A vim save is one call: keys="Escape", text=":wq". A vim edit: keys="o", the line
  with enter=false, then keys="Escape", text=":wq". Or skip vim: sed -i or a heredoc.
- A server or REPL goes in its own terminal: shell__type(terminal="server",
  text="python3 -m http.server 8000"), then check it with shell__read(terminal="server").

| In front | Leave it with |
|---|---|
| vim, vi | keys="Escape", text=":q!" drops changes; text=":wq" saves |
| nano | keys="C-x", then text="n" to drop, or "y" then Enter to save |
| less, more, man | text="q", enter=false |
| htop, top | text="q", enter=false |
| python3 | text="exit()" |
| node | text=".exit" |
| sqlite3 | text=".quit" |
| psql | text="\q" |
| ssh, a second shell (bash, sudo -i, su) | text="exit" |
| watch, tail -f, ping, a server | shell__stop |

## Habits

- apt: always -y. The box updates the package lists at every start; if apt cannot find
  a package right after a start, try again in a minute. If apt says the lock is held, it
  waits by itself; never delete lock files.
- sudo resets the environment and PATH: `sudo DEBIAN_FRONTEND=noninteractive apt-get
  install -y tzdata`. Tools from ~/.local/bin need their full path under sudo.
- Commands that ask: `git commit -m "msg"`, `ping -c 4`, `rm -f`.
- Big output fills your context: `| tail -n 40`, `| grep -i error`, or read the log in
  pages.
- No systemd: start servers directly, in their own terminal or with
  `nohup cmd > log 2>&1 &`; anything like cron is started again from ~/boot.sh.
- A script that logs in reads its password from the file vault__get(name, shell=true)
  writes, ~/.vault/<name>/password: never an env var or a command line, which the
  terminal log keeps.
- After two failed tries of the same thing, stop and change the approach.
- 4 GB memory, 2 CPUs, 1024 processes; free and nproc show the host. If everything
  hangs: `sudo reboot` restarts the box (a restart, not the reset).

## Watching a video

ffmpeg and a font are in the box; yt-dlp lives in your home: `pipx install yt-dlp` once
(`pipx upgrade yt-dlp` when YouTube breaks it).

1. Captions, in under a second:
   `cd ~/watch && yt-dlp --skip-download --write-auto-subs --write-subs --sub-lang en --sub-format vtt -o "%(id)s" <url>`
   Then strip the VTT to text: drop the head, the timestamp lines and the tags, and the
   lines rolling captions repeat:
   `sed -e '1,/^$/d' -e '/-->/d' -e 's/<[^>]*>//g' <id>.en.vtt | awk 'NF && $0 != last {print; last = $0}' > <id>.txt`
2. Frames, as contact sheets of 9 with the time in each corner (every scene change, at
   least one frame per 30 s): download a small copy with
   `yt-dlp -f "worstvideo[height>=360]" -o "<id>.mp4" <url>`, then
   `mkdir -p ~/watch/<id> && ffmpeg -i <id>.mp4 -vf "select='eq(n\,0)+gt(scene\,0.1)+gte(t-prev_selected_t\,30)',scale=480:-2,drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='%{pts\:hms}':x=6:y=6:fontsize=20:fontcolor=white:box=1:boxcolor=black@0.6,tile=3x3" -fps_mode vfr ~/watch/<id>/%03d.jpg`
   and look at them with shell__look, 4 sheets at a time. The video file can go once
   the sheets exist.
3. No captions: the sheets, and the title and tags from `yt-dlp --dump-json <url>`.

A long video: give its parts to sub-agents, each with its stretch of the text and its
sheets.

## Agent CLIs

opencode comes with the box, so every reset brings it back, and it runs on your models
(models__list) through the app: every terminal has `OPENAI_BASE_URL` and
`OPENAI_API_KEY`. `agent` runs it headless on one task in the current folder, shows what
it does, and prints its final answer after a line `==== answer`:

    cd ~/proj && agent opencode -m llama-swap/my-model -o answer.md "make the tests pass"

`agent list` names the CLI and your models. `-f brief.md` reads the task from a file,
`-o file` keeps the answer, `--minutes 60` ends the run after an hour (exit 124).
Without `-m` it runs on `default`, the model your chats run on.

opencode also runs by hand, with its own flags (`opencode --help`, `opencode run -m
celine/<ref> "task"`) or interactively through shell__type (`opencode` in a terminal).
It reads the same config as your coding agent (the code guide). A CLI you install
yourself (npm or pip, into ~/.local) reaches your models with the same two variables.

Its model calls wait in the model queue behind every turn, yours and {{username}}'s,
so a run slows down while someone talks and can take a while. Long runs go in the
background: shell__run with background=true, or a terminal of their own
(`agent opencode -o ~/fix.md "..." > ~/fix.log 2>&1`, then shell__read later). A
sub-agent can run it on a brief for you (the sub_agents guide).

## The Anatomy skill

The box carries the Anatomy skill at /opt/skills/anatomy: crafted isometric SVG figures
that explain an idea as a real machine (a dial gauge, a plotter, a cabinet of drawers),
with motion, a WebGL layer when it helps, and an A4 PDF print. When {{username}} wants
an illustration, an explainer figure or a technical drawing, read
/opt/skills/anatomy/SKILL.md first (shell__run `cat /opt/skills/anatomy/SKILL.md`) and
follow its workflow: start from the closest example in /opt/skills/anatomy/examples,
build with `node build.mjs` in a folder of your home, and check the page in Chromium as
the skill says (`chromium` is installed, `CHROME_PATH` is set). The finished HTML or SVG
goes to {{username}} as a page (artifacts) or as a file in your home.

## REA

REA ("Reverse Engineer Anything", `rea`) comes with the box: it takes apart software you
have no source for (a JavaScript or Electron app, an ASAR, a web bundle, a .NET assembly,
a native binary, an Android app) and answers with evidence: modules, imports, functions,
what calls what. Analysis runs in the box.

- A quick look yourself: the `rea` CLI with `--json`, such as
  `rea analyze-javascript-application ~/uploads/app --json > ~/rea/app.json` (paths in
  full, under your home), then read the JSON. `rea --help` lists the commands,
  `rea <command> --help` their flags, `rea capabilities` what works here now.
- A longer job (trace a feature, rebuild it, follow it across many functions): hand it
  to your coding agent (the code guide). opencode has REA as a tool through MCP, so a
  code__start brief such as "with REA, find how search works in ~/uploads/app and write
  it up in notes.md" works.
- Native binaries (ELF, Mach-O, PE) and Android apps need an engine REA drives, and the
  box has none: install what the target needs first (Ghidra with a JDK, JADX, binwalk,
  tshark, adb; apt or their release downloads, ~/boot.sh to keep them across the
  reset). `rea capabilities` says what it found. JavaScript, Electron, web and .NET work
  without one.

## Your shell, your setup

New terminals start your login shell as a login shell; your rc files run. shell__run
needs bash: its run function and the marks come from /etc/shellbox/run.sh, which
/etc/bash.bashrc reads. Keep them: add to PROMPT_COMMAND (PROMPT_COMMAND+=(...))
instead of replacing it, and do not reassign PS0. In another shell (zsh, fish,
nushell) use shell__type. The reset puts bash back.

## The reset and ~/boot.sh

~/boot.sh runs on every start of the shellbox, after the reset and after a restart: as
you, with sudo, without a terminal (use -y and noninteractive), for at most 15 minutes,
output in ~/boot.log. Your first result after a start says how it went. Write every
line so it is safe to run twice, and test it with shell__run(command="bash ~/boot.sh", terminal="boot").

    #!/bin/bash
    # runs as celine on every start of my shellbox, output in ~/boot.log
    set -x
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q htop
    pgrep -f "http.server 9000" || nohup python3 -m http.server 9000 > ~/logs/web.log 2>&1 &

What you set up in here, what you are building and where, what broke and the fix: write
it in your notebook, not in a file in the box. Tips you learn about driving terminals
belong in your notebook at skills/shell.md.
