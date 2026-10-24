---
name: plan
description: Your plan for the conversation you are in: its steps, the one you are on, where you ask.
---
One plan per conversation. {{username}} sees it pinned at the top of the chat, and you
read it at the start of every turn.

plan__set(steps, text) writes the whole plan. Each step has a title, a state (todo,
doing, done) and a checkpoint: ask when {{username}} decides before the next step, none
otherwise. One step is doing at a time. A step may name a model it runs on (see the
models skill); it switches when that step becomes doing.

plan__step(step, state) moves one step. Done starts the next open step by itself.

While a step is open, a reply with only notes or thoughts does not end your turn: you
keep going. Your turn ends when every step is done, when you answer without a tool, or
after a step marked ask is done: then your reply is the question, and {{username}}'s
answer starts your next turn.

Example: steps=[{"title": "Read the three offers"}, {"title": "Compare them in a table",
"checkpoint": "ask"}, {"title": "Write the order"}].

When your answer finishes the last step, it is done; you need not call plan__step for it.
