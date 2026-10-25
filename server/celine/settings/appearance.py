"""Settings, Appearance: accent colour, grain, tint, contrast, text size,
hide thinking, hide tool calls, and collapse thinking by default, in var/settings/appearance.toml
like every other section. The choices and defaults are APPEARANCE; a value never saved is its default."""
from __future__ import annotations

from typing import Any

from . import files
from .files import Invalid

# Settings, Appearance: key -> (choices, default). The one design;
# these only change colours, the canvas grain, the text size, whether its thinking and tool
# calls are hidden, and whether its thinking starts collapsed (stored and sent in
# GET /appearance and the config status). The numbers are percents: the grain's opacity,
# how much the accent tints the greys (tint) and the lightness step between surfaces
# (contrast), as web/src/styles/tokens.css reads them.
# a larger contrast step lifts the surfaces past the text contrast floor (web CONTRAST_MAX);
# a value saved above it, from before the cap, reads as the cap
CONTRAST_MAX = 110
ACCENTS = ("rose", "peach", "sage", "teal", "sky", "violet", "miku")
APPEARANCE: dict[str, tuple[tuple, Any]] = {
    "accent": (ACCENTS, "rose"),
    "grain": (("fine", "coarse", "paper", "none"), "fine"),
    "grain_opacity": (tuple(range(0, 21)), 4),
    "tint": (tuple(range(0, 301, 25)), 100),
    "contrast": (tuple(range(50, CONTRAST_MAX + 1, 10)), 100),
    "text_size": (("sm", "md", "lg", "xl"), "md"),
    "hide_thinking": ((False, True), False),
    "hide_tools": ((False, True), False),
    "collapse_thinking": ((False, True), True),
}


def validate(data: Any) -> dict[str, Any]:
    """Only the keys given; a value must be one of its choices, null puts the key back to its
    default (Reset to defaults sends every key as null)."""
    if not isinstance(data, dict):
        raise Invalid("appearance", "Appearance settings are an object.")
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k not in APPEARANCE:
            raise Invalid(f"appearance.{k}", f"'{k}' is not an appearance setting.")
        choices = APPEARANCE[k][0]
        # True == 1 == 1.0 in Python: a value takes only its choices' own type
        if v is None:
            out[k] = None
            continue
        if type(v) is not type(choices[0]) or v not in choices:
            raise Invalid(f"appearance.{k}", f"'{v}' is not a choice for {k}.")
        out[k] = v
    return out


def effective(saved: dict[str, Any]) -> dict[str, Any]:
    """Every key, its saved value or its default."""
    return {k: saved.get(k, d) for k, (_, d) in APPEARANCE.items()}


def read(root) -> dict[str, Any]:
    """The saved values from var/settings/appearance.toml (empty while it does not exist).
    A key the file holds that is no setting is left out, and gone with the next save."""
    raw, err = files.read(root, "appearance")
    if err:
        raise Invalid("appearance", err)
    raw = {k: v for k, v in (raw or {}).items() if k in APPEARANCE}
    if type(raw.get("contrast")) is int and raw["contrast"] > CONTRAST_MAX:
        raw["contrast"] = CONTRAST_MAX
    return {k: v for k, v in validate(raw).items()
            if v is not None}
