"""The agent's tools for its plan: plan__set writes it, plan__step moves one step. The rules are
plans/store.py's; this module only turns them into its words."""
from __future__ import annotations

from typing import Annotated, Literal, NotRequired, Required, TypedDict

from ..skills import ToolContext, ToolResult, guide, tool, described
from . import flow
from . import store as plans


class Step(TypedDict):
    title: Required[Annotated[str, "What this step does, a few words."]]
    state: NotRequired[Annotated[Literal["todo", "doing", "done"],
                                 "todo (default), doing (the one you are on) or done."]]
    checkpoint: NotRequired[Annotated[Literal["none", "ask"],
                                      "ask: when this step is done, you stop and ask "
                                      "{{username}} before the next one. none: you go "
                                      "on (default)."]]
    model: NotRequired[Annotated[str, "The model this step runs on, as the list of "
                                      "models names it (provider/model). Leave it out "
                                      "to keep the model you are on."]]


class Plan:
    name = "plan"
    guide = guide("plan")
    description = described()

    def __init__(self, cfg):
        self.cfg = cfg

    app = None      # the running app (core/services.py), from skills.build

    def _said(self, plan: dict, head: str) -> ToolResult:
        text = flow.block(plan) or ""
        return ToolResult(content=self.cfg.substitute(f"{head}\n{text}".strip()),
                          display={"plan": plan})

    @tool(read_only=False, destructive=False)
    async def set(
        self,
        ctx: ToolContext,
        steps: Annotated[list[Step], "Every step in order. One is doing; without one, "
                                     "the first open step is."],
        text: Annotated[str, "The plan in a few lines when the steps need more words: "
                             "what you deliver, what you decided. Empty: none."] = "",
    ) -> ToolResult:
        """Write your plan for this conversation, replacing the one before. {{username}}
        sees it pinned at the top of the chat. Use it for work of more than two steps;
        while a step is open you keep going after notes and thoughts."""
        before = await plans.get(ctx.store, ctx.conversation_id)
        try:
            clean = plans.clean(steps, (before or {}).get("steps") or ())
            flow.check_models(getattr(self.cfg, "settings", None), clean)
        except plans.Refused as e:
            return ToolResult(content=f"Plan not saved: {e}", is_error=True)
        plan = await plans.save(ctx.store, ctx.conversation_id, text.strip(), clean)
        model = await flow.follow_model(ctx.store, self.cfg, ctx.conversation_id, plan)
        head = f"Plan saved, {len(clean)} steps." + (
            f" You run on {model} from your next turn." if model else "")
        return self._said(plan, head)

    @tool(read_only=False, destructive=False)
    async def step(
        self,
        ctx: ToolContext,
        step: Annotated[int, "The step's number, 1 for the first."],
        state: Literal["doing", "done", "todo"],
    ) -> ToolResult:
        """Move one step of your plan. Done starts the next open step, except after a
        step marked ask: then your turn ends with your question to {{username}}, so say
        it in the same reply."""
        plan = await plans.get(ctx.store, ctx.conversation_id)
        if plan is None:
            return ToolResult(content="There is no plan here: plan__set first.",
                              is_error=True)
        try:
            steps, waiting = plans.move(plan, step, state)
        except plans.Refused as e:
            return ToolResult(content=f"Not moved: {e}", is_error=True)
        plan = await plans.save(ctx.store, ctx.conversation_id, plan["text"], steps, waiting)
        model = await flow.follow_model(ctx.store, self.cfg, ctx.conversation_id, plan)
        head = f"Step {step} is {state}." + (
            f" You run on {model} from your next turn." if model else "") + (
            " Your turn ends here: ask {{username}} now." if waiting else "")
        return self._said(plan, head)
