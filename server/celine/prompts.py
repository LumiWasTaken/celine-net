"""A text a model reads is code: a .md file in the package beside the module that reads
it (turns/title.md, kb/describe.md, skills/writer.md, ...), read once. `text(__file__,
"name.md")` is that file; `fill` puts the code's values into its `{{placeholders}}`,
then the names and pronouns (config.substitute). A placeholder nothing fills is a Problem
(cfg.problems, the Settings notice) and the text goes out as it is.

The texts a user edits are not here: its persona, talk and voice are its row (her.py),
a Routine's prompts its schedule, Bastet's config/guard/ (guard/prompts.py), the notebook
seed config/notebook_seed/ (skills/notebook.py).
"""
from __future__ import annotations

import re
from functools import cache
from pathlib import Path

from .config import Problem

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
# config.substitute's own, filled when a request is built
SUBSTITUTED = frozenset(("username", "user", "name", "char", "agent_name", "she", "her", "herself",
                         "them", "She", "Her", "Herself", "Them"))


@cache
def text(module_file: str, name: str) -> str:
    """The package file `name` beside the module at `module_file` (its __file__)."""
    return (Path(module_file).parent / name).read_text()


def fill(cfg, module_file: str, name: str, **values) -> str:
    """`name` beside `module_file` with every `{{key}}` of `values` in, then the names
    and pronouns (cfg.substitute). A `{{placeholder}}` left over is a Problem, once."""
    out = text(module_file, name)
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", str(value))
    if cfg is not None:
        out = cfg.substitute(out)
        left = sorted({m for m in _PLACEHOLDER.findall(out) if m not in SUBSTITUTED})
        if left:
            where = f"{Path(module_file).parent.name}/{name}"
            p = Problem(where, f"nothing fills {{{{{left[0]}}}}}")
            if p not in cfg.problems:
                cfg.problems.append(p)
    return out
