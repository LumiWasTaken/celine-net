"""The agent's own model access: the models it may use (settings/her_models.py), and one
request to one of them, text in and text out (models__list, models__ask).

The request waits in the app's model queue like its turn's own calls, at its turn's
priority, and runs on exactly the model it names: when it fails, the call fails with
its error, no other model is tried. Thinking stays the model's own; the text alone
comes back."""
from __future__ import annotations

import json
import math
from typing import Annotated, Literal, TypedDict

from ..backends.base import BackendError
from ..backends.queue import Priority
from ..settings import her_models
from ..turns.compact import complete
from ..turns.prompt import model_facts
from . import ToolContext, ToolResult, guide, tool, described


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
        answers and whether the model is loaded, each as its server last said."""
        s = self.cfg.settings
        refs = her_models.allowed(s)
        if not refs:
            return ToolResult(content="No models are open to you right now.")
        llm = self.app.llm if self.app is not None else None
        if llm is None:
            return ToolResult(content="\n".join(f"{r}: {her_models.label(s, r)}."
                                                for r in refs), display={"models": refs})
        hosts = llm.router.hosts()
        loaded = {m["ref"]: m["loaded"] for m in llm.router.loaded_view()["models"]}
        up = {"up": "Its server answers.", "down": "Its server does not answer."}

        def line(r: str) -> str:
            facts = [f"{r}: {her_models.label(s, r)}.", *model_facts(llm.make(r)),
                     up.get((hosts.get(s.split(r)[0]) or {}).get("state"), "")]
            if r in loaded:
                facts.append("It is loaded." if loaded[r] else "It is not loaded.")
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
        text. Example: models__ask(model="llama/my-model", messages=[{"role": "user",
        "content": "Three names for a grey cat."}])
        Returns: the answer alone."""
        s = self.cfg.settings
        allowed = her_models.allowed(s)
        if model not in allowed:
            return ToolResult(content=f"{model} is not one of your models. Yours: "
                                      f"{', '.join(allowed) or 'none'}.", is_error=True)
        wire = _messages(messages)
        if isinstance(wire, str):
            return ToolResult(content=wire, is_error=True)
        if system:
            wire = [{"role": "system", "content": system}, *wire]
        llm = self.app.llm if self.app is not None else None
        if llm is None:
            return ToolResult(content="The app's model layer is not running.", is_error=True)
        body = {"temperature": float(temperature)} if temperature is not None else None
        qb = llm.bind(llm.make(model), role="tasks", body=body,
                      priority=llm.turn_priority.get(ctx.turn_id, Priority.AGENT),
                      conversation_id=ctx.conversation_id, turn_id=ctx.turn_id,
                      label=f"models__ask {model}", fallback=False)
        try:
            text = await complete(qb, wire, math.inf, errors=True) or ""
        except BackendError as e:
            return ToolResult(content=f"{model} failed: {e}", is_error=True)
        if not text.strip():
            return ToolResult(content=f"{model} answered nothing.", is_error=True)
        return ToolResult(content=text, display={"model": model, "chars": len(text)})
