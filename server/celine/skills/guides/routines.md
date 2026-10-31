---
name: routines
description: The Routines: what runs on its own at a time, read, change and start them.
---
A Routine is a prompt that runs on its own at a time of day (or a cron line), in a
conversation of its own; {{username}} sees them on the Routines page.

routines__list lists them. routines__read(name) gives one whole: every setting, the
prompt, the reviewer prompt and its newest runs. routines__write makes or changes one
with the checks of the Routines page: a change changes only what you give (a prompt you
give replaces the old one whole, so read it first), a new one needs label, prompt and
time or cron. When it is not saved, nothing changed: the error names the field and what
it takes. routines__run(name) starts a run now. What you change and start shows as yours
in Events and in the routine's history.

A run has an address on the intercom: run:<id> (routines__read lists them), or
routine:<name> for its newest run. Write there to reach a run while it works.

A prompt may hold {{date}}, {{day_before}}, {{weekday}}, {{mood}}, {{on_mind}} and
{{result:<routine>}} (that routine's answer of the same night).
