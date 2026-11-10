"""The agent's own model access: the models it may use (settings/agent_models.py), and one
request to one of them, text in and text out (models__list, models__ask).

The request waits in the app's model queue like its turn's own calls, at its turn's
priority, and runs on exactly the model it names: when it fails, the call fails with
its error, no other model is tried. Thinking stays the model's own; the text alone
comes back. A cloud model the user did not pick waits for their approval at each call
(agent_models.ask_cloud)."""
from __future__ import annotations

import json
import math
from typing import Annotated, Literal, TypedDict

from ..backends.base import BackendError
from ..backends.queue import Priority
from ..settings import agent_models
from ..turns.compact import complete
from ..turns.prompt import model_facts
from . import ToolContext, ToolResult, guide, tool, described


ASK = "send messages to a cloud model"     # the approval card's action


class Message(TypedDict):
    role: Annotated[Literal["user", "assistant"], "Who says it."]
    content: Annotated[str, "The text."]


def _messages(value) -> list[dict] | str:
    """The agent's messages as the request takes them, or the sentence why not. A JSON string
    of them counts as them; any other text is one user message."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [{"role": "user", "content": value}] if value.strip() else \
                "messages is empty: give at least one user message."
    if not isinstance(value, list) or not value:
        return "messages is a list like [{\"role\": \"user\", \"content\": \"...\"}]."
    out = []
    for m in value:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") \
                or not isinstance(m.get("content"), str):
            return ("Each message is {\"role\": \"user\" or \"assistant\", \"content\": "
                    "\"text\"}.")
        out.append({"role": m["role"], "content": m["content"]})
    return out


class Models:
    name = "models"
    guide = guide("models")
    description = described()

    def __init__(self, cfg):
        self.cfg = cfg
        self.app = None

    @tool(read_only=True)
    async def list(self, ctx: ToolContext) -> ToolResult:
        """The models you may use: in models__ask, as a sub-agent's model, and in your
        shellbox's agent CLIs. Returns one line per model: its name to use, what it is,
        its context window, whether it sees images and calls tools, whether its server
        answers and whether the model is loaded, each as its server last said, why the
        model does not answer now when its last answer was a failure, and whether using
        it asks {{username}} first (a cloud model they did not pick)."""
        s = self.cfg.settings
        refs = agent_models.allowed(s)
        if not refs:
            return ToolResult(content="No models are open to you right now.")
        own = s.conversation_models.get(ctx.conversation_id)
        asks = {r: f"Using it asks {self.cfg.user} first." for r in refs
                if agent_models.cloud(s, r) and not agent_models.picked_by_user(s, r, own)}
        llm = self.app.llm if self.app is not None else None
        if llm is None:
            return ToolResult(content="\n".join(
                " ".join(f for f in (f"{r}: {agent_models.label(s, r)}.", asks.get(r)) if f)
                for r in refs), display={"models": refs})
        hosts = llm.router.hosts()
        loaded = {m["ref"]: m["loaded"] for m in llm.router.loaded_view()["models"]}
        up = {"up": "Its server answers.", "down": "Its server does not answer."}

        def line(r: str) -> str:
            facts = [f"{r}: {agent_models.label(s, r)}.", *model_facts(llm.make(r)),
                     up.get(hosts.get(s.split(r)[0]) or "", "")]
            if r in loaded:
                facts.append("It is loaded." if loaded[r] else "It is not loaded.")
            facts.append(llm.router.problem(None, r) or "")
            facts.append(asks.get(r, ""))
            return " ".join(f for f in facts if f)
        return ToolResult(content="\n".join(line(r) for r in refs), display={"models": refs})

    @tool()
    async def ask(
        self,
        ctx: ToolContext,
        model: Annotated[str, "One of your models, as models__list names it."],
        messages: Annotated[list[Message], "The conversation so far, ending with the "
                                           "message it answers."],
        system: Annotated[str | None, "A system prompt."] = None,
        temperature: Annotated[float | None, "0 to 2. Without it, the model's own."] = None,
    ) -> ToolResult:
        """Ask one of your models directly: your messages go in, its answer comes back as
        text. Example: models__ask(model="local/my-model", messages=[{"role": "user",
        "content": "Three names for a grey cat."}])
        Returns: the answer alone."""
        s = self.cfg.settings
        allowed = agent_models.allowed(s)
        # the row names the model as Settings labels it (display); the result keeps the
        # ref, which is what the agent calls it by
        shown = {"model": model, "label": agent_models.label(s, model) if model in allowed
                 else model}
        if model not in allowed:
            return ToolResult(content=f"{model} is not one of your models. Yours: "
                                      f"{', '.join(allowed) or 'none'}.", is_error=True,
                              display=shown)
        wire = _messages(messages)
        if isinstance(wire, str):
            return ToolResult(content=wire, is_error=True, display=shown)
        if system:
            wire = [{"role": "system", "content": system}, *wire]
        llm = self.app.llm if self.app is not None else None
        if llm is None:
            return ToolResult(content="The app's model layer is not running.", is_error=True,
                              display=shown)
        if no := await agent_models.ask_cloud(
                self.cfg, self.app, ctx, model, action=ASK, detail=("Messages", "\n\n".join(
                    f"{m['role']}: {m['content']}" for m in wire))):
            return ToolResult(content=no, is_error=True, display=shown)
        body = {"temperature": float(temperature)} if temperature is not None else None
        qb = llm.bind(llm.make(model), role="tasks", body=body,
                      priority=llm.turn_priority.get(ctx.turn_id, Priority.AGENT),
                      conversation_id=ctx.conversation_id, turn_id=ctx.turn_id,
                      label=f"models__ask {model}", fallback=False)
        try:
            text = await complete(qb, wire, math.inf, errors=True) or ""
        except BackendError as e:
            return ToolResult(content=f"{model} failed: {e}", is_error=True, display=shown)
        if not text.strip():
            return ToolResult(content=f"{model} answered nothing.", is_error=True,
                              display=shown)
        return ToolResult(content=text, display={**shown, "chars": len(text)})
