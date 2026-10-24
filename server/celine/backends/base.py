"""Backend events. Every backend yields BackendEvents, so nothing above this layer
knows which server answered."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# the app's mark on the tail's content block (turns/context.py): the Anthropic backend
# puts a cache breakpoint on it, the others leave it out of what they send
TAIL = "tail"
# where a request message came from (turns/context.py origin): the app's own key, no
# backend sends it
ORIGIN = "origin"


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    index: int
    id: str | None = None
    name: str | None = None
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0           # all of the prompt, cached parts included
    output_tokens: int = 0
    cache_read: int = 0             # of input_tokens: read from the provider's prompt cache
    cache_write: int = 0            # of input_tokens: written to it


@dataclass(frozen=True, slots=True)
class Done:
    reason: Literal["stop", "length", "tool_calls", "content_filter"]
    usage: Usage | None = None
    # the reply as the server reported it (backends/openai.py raw_reply), for the
    # transcript's raw response view
    raw: dict | None = None
    # the thinking blocks the provider signed ({text, signature}, or {data} when
    # redacted): they go back with the reply when a tool loop goes on (Anthropic)
    thinking: list | None = None


BackendEvent = TextDelta | ReasoningDelta | ToolCallDelta | Done


class BackendError(Exception):
    """Carries copy the UI can show as-is: names the thing, gives the number."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable
        self.raw: dict | None = None        # the server's error answer, when it gave one
        self.code: str | None = None        # context_overflow, unsupported_input, out_of_credits
        self.status: int | None = None      # the HTTP status, when there was one
        self.retry_after: float | None = None   # seconds from the Retry-After header
        self.unreachable = False            # the connection was refused
        self.n_ctx: int | None = None       # context_overflow: the model's context
        self.n_prompt_tokens: int | None = None
