"""Reciprocal rank fusion: rank-based, so bm25 and cosine never need calibrating."""
from __future__ import annotations

from collections.abc import Hashable, Sequence


def rrf(lists: Sequence[Sequence[Hashable]], k: int = 60,
        weights: Sequence[float] | None = None) -> list[tuple[Hashable, float]]:
    """score(d) = sum over lists of w / (k + rank), rank from 1. Ties keep first-seen
    order, so the result is deterministic."""
    scores: dict[Hashable, float] = {}
    for i, lst in enumerate(lists):
        w = 1.0 if weights is None else float(weights[i])
        seen = set()
        for rank, item in enumerate(lst, start=1):
            if item in seen:          # a list that repeats an item counts it once
                continue
            seen.add(item)
            scores[item] = scores.get(item, 0.0) + w / (k + rank)
    order = {item: n for n, item in enumerate(scores)}
    return sorted(scores.items(), key=lambda kv: (-kv[1], order[kv[0]]))
