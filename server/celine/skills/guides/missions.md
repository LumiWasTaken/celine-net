---
name: missions
description: A brief from {{username}}, worked on in a conversation of its own.
---
A mission is a brief from {{username}} in your instructions, with the steps it goes by.
Your plan is the plan skill's: plan__set writes the steps, plan__step moves one;
{{username}} sees it on the mission's page and at the top of its chat.
missions__update(status, note) records where you are; with Done, artifact names what you
delivered: artifact:<id> for a page, documents:<path> for a file in Documents. Your
notes, your report and what you deliver hold what you found in your own words: no web
citation marks, no "according to", no list of sources or links, a link only when it is
what {{username}} asked for. missions__list lists every mission, from anywhere.

missions__start(brief) starts a mission of your own, from anywhere but a sub-agent: it
takes what {{username}}'s form takes (name, model, time_limit, min_time, check_in,
report_to) and runs in a conversation of its own, its brief from you.

With a minimum time, missions__update(status="Done") before it is refused with the time
left; force=true closes it all the same.

A step may name a model it runs better on; your instructions list the models you can
name. When that step becomes doing, the mission runs on it from your next turn, and
sub-agents you start then run on it too. Example: steps=[{"title": "Read the papers",
"model": "lan/small"}, {"title": "Write the report"}].

When a turn ends while the mission is Active, a step of your plan is open and nothing is
set to wake you (no wake you set, no job or sub-agent still working, no message
waiting), the app writes one line that starts your next turn. When that turn calls no
tool, or every step is done and you did not close it, the mission is set to Needs you.
After a step marked ask is done, it waits on {{username}}'s answer.

When you wait on something (a download, a build, a page that changes, a time of day),
missions__wait(why, in="20 min") or at="06:00" ends your turn and starts the next one
then, with why as its first line. It costs nothing while you wait; checking again and
again costs the night. A job's end, a sub-agent's report or a message wakes you earlier.
With a time limit, a notice tells you to wrap up shortly before it: deliver, report and
close the mission then. Done while a step is still open needs a note on why.
