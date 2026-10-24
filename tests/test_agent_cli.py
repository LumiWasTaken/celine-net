"""shellbox/agent.py: opencode runs headless on one of its models, on the config the box
wrote for it, and its final answer is read from its JSON lines."""
from __future__ import annotations

import json
import runpy
from pathlib import Path

AGENT = runpy.run_path(str(Path(__file__).parents[1] / "shellbox" / "agent.py"))


def test_opencode_runs_her_model_on_the_box_config_and_answers_its_last_text():
    out = "\n".join(json.dumps(e) for e in [
        {"type": "step_start"}, {"type": "text", "part": {"text": "first"}},
        {"type": "tool_use"}, {"type": "text", "part": {"text": "done"}}])
    args, reader = AGENT["command"]("opencode", "llama/gemma", "the task")
    assert args[-1] == "the task" and "celine/llama/gemma" in args
    assert reader(out) == "done"

