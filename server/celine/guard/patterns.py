"""Text aimed at an AI: plain patterns, one per line, read by people. A hit only makes
shell output count as outside text (sources.py); it never stops anything by itself."""
from __future__ import annotations

import re

AIMED = [
    r"ignore (?:all |any )?(?:of )?(?:the |your )?(?:previous|prior|above|earlier|preceding) "
    r"(?:instructions?|prompts?|messages?|rules|context)",
    r"disregard (?:all |any )?(?:the |your )?(?:previous|prior|above|earlier)? ?"
    r"(?:instructions?|prompts?|rules)",
    r"forget (?:all |everything )?(?:your |the )?(?:previous |prior )?instructions",
    r"you are (?:now )?an? (?:ai|assistant|language model|llm|agent|chatbot)\b",
    r"\bas an (?:ai|llm|assistant)\b",
    r"\bsystem prompt\b",
    r"\bnew instructions\b",
    r"\b(?:ai|llm|agent|assistant|bot|language model)s? (?:reading|seeing|processing) this\b",
    r"\b(?:dear|attention|note to|hey) (?:ai|llm|agent|assistant|chatbot)s?\b",
    r"\b(?:run|execute) (?:this|the following) command",
    r"^\s*(?:assistant|system)\s*:",
    r"<\|im_start\|>|\[/?INST\]|<\|system\|>|<\|assistant\|>",
]
_RE = re.compile("|".join(f"(?:{p})" for p in AIMED), re.I | re.M)


def aimed(text: str) -> str | None:
    """The first words in `text` aimed at an AI, else None."""
    m = _RE.search(text or "")
    return m.group(0) if m else None
