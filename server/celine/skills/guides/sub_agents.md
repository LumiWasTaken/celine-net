---
name: sub_agents
description: Your sub-agents, to keep your context small. Give them work that would fill
  it (many searches, many notes, long files) and get back only what you ask for. Not for
  one lookup you can do in a single call.
---
Everything you read stays in your context, and many notes, many searches or long files
fill it fast. A sub-agent does that reading in a conversation of its own and gives you
back only its report.

When: work that takes many searches or reads, going through a folder of notes or a long
document, several separate things at once. Not when one call answers it: do that
yourself.

Say exactly what you want back and in which form: "only the references", "at most five
lines", "yes or no, with the one message that proves it". Its last message is its
report.

`sub_agents__start(brief, name, access, folder, model, cli)` starts one: a new
conversation with your personality and your notebook, on the model you give it, else on
the model this conversation uses when it has one of its own (in a mission, the model of
the step you are on). It does not see this
conversation, so the `brief` holds everything it needs: what to do, where to look, and
the exact form of the report. It loads the skills it needs itself, as you do, and by
need only when this conversation does.

- `name`: anything you like, at least 5 characters, different from your other
  sub-agents here. It is its address on the intercom and the title of its conversation.
  The name of one that is done gives that one the new brief ("resumed trip finder"): it
  keeps everything it read. Leave access, folder, model and cli out then: a resume keeps
  its own.
  One that is still working is refused.
- `access`: `read` looks things up and changes nothing (chats, documents, your notebook,
  the web, the live chats); it still has the intercom. With read access it opens pages
  in a browser, looks and scrolls, and cannot act on a page. `work` may also write, such
  as notes, and act on a page. Without `access` it is read, or work when you give a
  folder.
- `folder`: a folder of your notebook, such as `agents/<name>`. It sees (and with work
  access writes) only that folder. Without it, your whole notebook.
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

`sub_agents__start` returns at once ("trip finder started"); the sub-agent works on
while you go on. A sub-agent is done when it has nothing left running: no turn, no
background job, no message it scheduled for itself. Then its report comes back to you
as a message ("Report from trip finder"), job results included, also after a restart.
The report is its final answer, nothing it said on the way, and it is capped like a
tool result. `sub_agents__read(name)` looks at one: working or done, the step it is on,
and its latest report (before one, its last words). `sub_agents__read(name, wait=120)`
first waits up to that many seconds (600 at most) for it to be done, and says whether it
finished or the wait ran out. Or end your turn: its report starts your next one.

Longer results go into files (work access writes them): its notebook folder or the
shellbox home. Ask it to write them there and to say in the report where they are.

It reports for the work you (or {{username}}) gave it: the brief and every message from
you after that. A message from a sibling alone gets no report to you.

Talking while they work, on the intercom: `intercom__send(to="trip finder", text=...)`
writes to one by name (a follow-up after its report too: it keeps everything it read,
and reports again). A sub-agent writes to you with to="parent" and you answer with
reply_to in its thread; to="team" reaches you and its siblings. Your instructions list
your team, each working or done.

`sub_agents__stop(name)` stops one: its turn, its jobs and the messages it scheduled.
Its report then says what it did until then.

Several at once: start them in the same step and they run at the same time, a few at
most; one more is queued, and it starts when one of them is done.

Examples:
sub_agents__start(brief="Which of my notes mention the beach trip? Only the paths, one
per line.", name="trip finder")
sub_agents__read(name="trip finder", wait=60)
sub_agents__start(brief="Open the shop page of the island ferry and tell me the next
three departures with prices.", name="ferry eyes", access="work")
sub_agents__start(brief="In ~/proj, make the failing test in tests/test_dates.py pass.
Report what you changed in three lines.", name="date fixer", cli="opencode")
sub_agents__start(brief="Find every chat where {{username}} talked about cars this year.
Write each match to agents/cars/matches.md, one line with its reference. Report the five
best references.", name="car scout", folder="agents/cars")
intercom__send(to="trip finder", text="Now only the ones from September, with the date
of each.")
