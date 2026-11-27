"""Celine's retrieval: one pipeline for `find`, plus exact counts and slices.

    scope.resolve(...)      people, chats, when → Scope (references only, a name is refused)
    pipeline.retrieve(...)  variants → lexical ‖ dense → RRF → rerank → episodes
    count.count(...)        exact SQL aggregates, incl. co-participants
    episodes.render(...)    `[kb:msg:id]` on every line
"""
from .pipeline import Result, Settings, retrieve
from .rerank import HttpReranker, RerankError, Reranker
from .scope import Scope, resolve

__all__ = ["HttpReranker", "RerankError", "Reranker", "Result", "Scope",
           "Settings", "resolve", "retrieve"]
