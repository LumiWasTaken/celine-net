---
name: models
description: Your own model access. Lists the models you may use and asks one of them
  directly, for a second opinion, a draft, a quick translation or a test of a prompt.
---
`models__list()` names the models you may use, one per line: the name to use, what
it is, its context window, whether it sees images and calls tools, whether its server
answers and whether it is loaded. The same names work in `models__ask`, as a sub-agent's `model`, and in the
agent CLIs in your shellbox. {{username}} sets which ones are open to you.

`models__ask(model, messages, system, temperature)` sends one request and gives you its
answer as text.

- `model`: one of yours, as models__list names it.
- `messages`: the conversation so far, ending with what it answers:
  `[{"role": "user", "content": "..."}]`, with `assistant` messages in between when you
  want to show it earlier answers.
- `system`: a system prompt, when you want one.
- `temperature`: 0 to 2; without it, the model's own.

The model sees exactly what you send: none of your notes, none of this conversation. The
request waits in the model queue with your other calls and runs on exactly the model you
name. When that model fails, the call fails and says why.

Example:
models__ask(model="llama/my-model", system="Answer in one line.", messages=[{"role":
"user", "content": "What rhymes with orange?"}])
