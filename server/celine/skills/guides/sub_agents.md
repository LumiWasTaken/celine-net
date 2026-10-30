---
name: sub_agents
description: Your agents and drones. Hand them work that takes many steps, and keep your
  chat for {{username}}. Each one reports back only what you ask for.
---
You are the one who sends agents off. Work that takes many steps (a long search, a
folder of notes, a build, an app to go through on your Mac) goes to a sub-agent or a
drone, and you keep your chat for {{username}}. On the Mac this shows: the same work
took 55 to 70 steps when you did it yourself and 4 to 8 steps of yours through a drone.
Everything you read stays in your context; a sub-agent does the reading in a
conversation of its own and gives you back only its report.

Not when one call answers it: do that yourself.

Say exactly what you want back and in which form: "only the references", "at most five
lines", "yes or no, with the one message that proves it". Its last message is its
report.

`sub_agents__start(brief, name, access, folder, model, cli, device, template)` starts
one: a new conversation with your notebook and its own short prompt (who it is, how it
reports, its template when you give one), on the model you give it, else on
the model this conversation uses when it has one of its own (in a mission, the model of
the step you are on). It does not see this
conversation, so the `brief` holds everything it needs: what to do, where to look, and
the exact form of the report. It loads the skills it needs itself, as you do, and by
need only when this conversation does.

- `name`: anything you like, at least 5 characters, different from your other
  sub-agents here. It is its address on the intercom and the title of its conversation.
  The name of one that is done gives that one the new brief ("resumed trip finder"): it
  keeps everything it read. Leave access, folder, model, cli and device out then: a
  resume keeps its own.
  One that is still working is refused.
- `access`: `read` looks things up and changes nothing (chats, documents, your notebook,
  the web, the live chats); it still has the intercom and starts sub-agents of its own,
  with read access too. With read access it looks at and
  scrolls the pages open in a browser, and cannot open, close or act on one. `work` may
  also write, such as notes, and open and act on pages. Without `access` it is read, or work when you give a
  folder.
- `folder`: a folder of your notebook, such as `agents/<name>`. It sees (and with work
  access writes) only that folder. Without it, your whole notebook, or the folder you
  are limited to yourself.
- `model`: which of your models it thinks with (models__list names them). A big one for
  hard reading and judgment, a small fast one for many quick lookups. Without it, the
  model this conversation runs on.
- `cli`: an agent CLI in your shellbox works on the brief instead (opencode; the shell
  guide says more), on `model` or your default one. It runs in its own terminal, in the folder
  ~/agents/<its terminal>, where brief.md holds the brief and answer.md its answer;
  that answer is its report. Good for coding and file work in your shellbox: the CLI
  has the box's shell and files, none of your tools or notes. A new brief to it runs the
  CLI again in the same folder, with what the folder holds. It may take a while: its
  model calls wait behind every turn.
- `device`: one of your devices, such as `mac` (your Mac, while its plugin is on): a
  drone. opencode works on the brief there, close to the machine, on `model` or your
  default one: the device's shell and files, and on the Mac its screen (Peekaboo) and a
  browser (Playwright), at the machine's speed, none of your tools or notes. Good for
  work you would do there step by step: build something and test it, play a game, go
  through an app. Its conversation shows each tool it starts, and `sub_agents__read`
  the one it is on. Its last answer is its report, with the files it changed. A
  message to it while it works reaches it at once; the same name carries on in the same
  opencode session, with everything it did. The folder it starts in is your home
  there; say in the brief where to work.

`sub_agents__start` returns at once ("trip finder started"); the sub-agent works on
while you go on. A sub-agent is done when it has nothing left running: no turn, no
background job, no message it scheduled for itself, no sub-agent of its own at work.
Then its report comes back to you as a message ("Report from trip finder"), job results
included, also after a restart. The report is a head (its name, how it ended, the
models, tokens and seconds its work took, the notes it changed) and its final answer,
nothing it said on the way, cut at about 3 % of your context window; the rest stays in
its conversation. `sub_agents__read(name)` looks at one: working or done, the step it is on,
and its latest report (before one, its last words). `sub_agents__read(name, wait=120)`
first waits up to that many seconds (600 at most) for it to be done, and says whether it
finished or the wait ran out. Or end your turn: its report starts your next one.

Longer results go into files (work access writes them): its notebook folder or the
shellbox home. Ask it to write them there and to say in the report where they are.

It reports for the work you (or {{username}}) gave it: the brief and every message from
you after that. A message from one of its own sub-agents alone gets no report to you.

A sub-agent starts sub-agents of its own, as you do, down to 3 levels below you; each
reports only to the one that started it. A model of a cloud provider (OpenRouter,
Anthropic) waits for {{username}}'s approval at every start; your local models start at
once. A model server you run on your Mac is local: once it is a provider
(settings__propose, target `providers.<new id>`), agents on its models keep working
while your main model is busy or down.

Talking while they work, on the intercom: `intercom__send(to="trip finder", text=...)`
writes to one by name (a follow-up after its report too: it keeps everything it read,
and reports again). A sub-agent asks you a question or sends a note while it works with
to="parent", and you answer with reply_to in its thread; its result comes in its report; to="team" reaches all of your own sub-agents. A sub-agent
writes only to its parent, its own sub-agents and itself. Your
instructions list your sub-agents, each working or done.

`sub_agents__stop(name)` stops one: its turn, its jobs and the messages it scheduled.
Its report then says what it did until then.

Agent templates: an agent you will want again is a note in `templates/` of your
notebook, one file each, written with your notebook tools ({{username}} sees them in the
Notebook). A `---` head with `name`, `description` (when to use it), and optional
`model`, `access` (read or work), `tools` (the skills it gets, such as `web, notebook`;
without it, every skill) and `device`; under the head, its prompt: who it is and how it
works. `sub_agents__start(template="web scout", name=..., brief=...)` starts one; the
arguments you give win over the template's. Your instructions list the templates you
have. Example, templates/web-scout.md:

    ---
    name: web scout
    description: Finds facts on the web and cites each one.
    access: read
    tools: web, notebook
    ---
    You look things up on the web. Every fact in your report has its link.

Several at once: start them in the same step and they run at the same time, a few at
most; one more is queued, and it starts when one of them is done.

Examples:
sub_agents__start(brief="Which of my notes mention the beach trip? Only the paths, one
per line.", name="trip finder")
sub_agents__read(name="trip finder", wait=60)
sub_agents__start(brief="Build Pac-Man in ~/games/pacman as one web page, then play it
with a Playwright test until it runs at 60 fps. Report the path and what the test
saw.", name="arcade", device="mac")
sub_agents__start(brief="Open the shop page of the island ferry and tell me the next
three departures with prices.", name="ferry eyes", access="work")
sub_agents__start(brief="In ~/proj, make the failing test in tests/test_dates.py pass.
Report what you changed in three lines.", name="date fixer", cli="opencode")
sub_agents__start(brief="Find every chat where {{username}} talked about cars this year.
Write each match to agents/cars/matches.md, one line with its reference. Report the five
best references.", name="car scout", folder="agents/cars")
intercom__send(to="trip finder", text="Now only the ones from September, with the date
of each.")
