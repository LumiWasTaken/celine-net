---
name: bugreport
description: Problems with the app itself, filed for {{username}} to review later.
---
`bugreport__file(title, severity, what, where, wish)` files one problem with the app:
a tool that fails or misleads, a limit that is wrong, something you miss. File it when
you hit it, from any conversation or routine; it does not interrupt anything.

- One report per problem. The result lists your other open reports: do not file one
  twice.
- `severity`: high stops your work, medium you work around, low is a small thing.
- `what`: what happened and what you expected, with the error text. `where`: the tool,
  routine or conversation and message number. `wish`: what would fix it.

Each report goes to {{username}} at once as an intercom message from the conversation
you filed it in, its own thread. Their reply reaches you there; they close the report
when it is fixed, or continue the thread in a chat with you.
