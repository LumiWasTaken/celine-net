"""The lowest reranker score (`chat_archive.rerank_floor`), measured: made-up chat passages
in the shape the index holds, questions that have an answer among them and questions that
have none, scored on the real reranker. The floor has to sit between the two groups.

    CELINE_LIVE_RERANK=https://host/v1/rerank CELINE_LIVE_RERANK_MODEL=bge-reranker-v2-m3 \\
    CELINE_LIVE_KEY=... CELINE_LIVE_FLOOR=0.018 uv run pytest -m live tests/test_live_floor.py -s
"""
from __future__ import annotations

import os

import pytest

from celine.kb.retrieve.rerank import HttpReranker

pytestmark = pytest.mark.live
URL = os.environ.get("CELINE_LIVE_RERANK")
if not URL:
    pytest.skip("set CELINE_LIVE_RERANK to run against a real reranker",
                allow_module_level=True)

PASSAGES = [
    'Discord chat with Moon (username shiniloli)\nMoon (username shiniloli), me\n'
    'Friday 20 November 2026, evening\nMoon: my cat knocked the monstera off the shelf '
    'again\nme: is the pot broken\nMoon: no but there is soil everywhere',
    'WhatsApp group "Chess club"\nAlex (username alex_k), Bea, me\nTuesday 14 November '
    '2023, evening\nAlex: I lost on time in a winning rook endgame\nBea: you always play '
    'too slow in the opening\nme: blitz on thursday?',
    'WhatsApp chat with Noor\nNoor, me\nSunday 2 May 2023, morning\nNoor: ok fine, Portugal '
    'it is, but I pick the hotel\nme: deal, flights are cheaper in September\nNoor: book '
    'them before I change my mind',
    'Discord server "Garage" #engine\nRico (username rico_77), me\nMonday 3 March 2025, '
    'night\nRico: the head gasket is done, coolant in the oil\nme: how much for the '
    'repair\nRico: 900 with parts, two days',
    'WhatsApp chat with Mama\nMama, me\nSaturday 24 December 2022, afternoon\nMama: bring '
    'the big pot, we are twelve tonight\nme: do you need more potatoes\nMama: yes and '
    'butter',
    'Discord chat with Vega (username vega_x)\nVega (username vega_x), me\nWednesday 9 July '
    '2025, night\nVega: ich kann nicht schlafen, morgen ist die Prüfung\nme: du hast doch '
    'gelernt\nVega: ja aber Statistik macht mir Angst',
]
ANSWERED = [                                   # (question, the passage that answers it)
    ("Moon's cat makes a mess with a houseplant", 0),
    ("someone loses a chess game because the clock ran out", 1),
    ("Noor gives in about the trip to Portugal", 2),
    ("a car needs an expensive engine repair", 3),
    ("planning the food for a big family dinner at Christmas", 4),
    ("Vega is nervous before an exam and cannot sleep", 5),
    ("what did shiniloli say about the plant", 0),
    ("jemand hat Angst vor einer Prüfung", 5),
]
UNANSWERED = [
    "someone announces they are pregnant",
    "a discussion about buying bitcoin",
    "complaints about a landlord raising the rent",
    "the football match on Saturday",
    "a recipe for sourdough bread",
    "Moon talks about her new job at the hospital",
    "booking a hotel in Tokyo",
    "jemand hat sein Handy verloren",
]


def test_the_floor_separates_what_fits_from_what_does_not():
    r = HttpReranker(URL, os.environ.get("CELINE_LIVE_RERANK_MODEL", "bge-reranker-v2-m3"),
                     key=os.environ.get("CELINE_LIVE_KEY"),
                     scale=os.environ.get("CELINE_LIVE_SCALE", "relevance"))
    fits = [r.score(q, PASSAGES)[i] for q, i in ANSWERED]
    others = [s for q, i in ANSWERED for k, s in enumerate(r.score(q, PASSAGES)) if k != i]
    none = [max(r.score(q, PASSAGES)) for q in UNANSWERED]
    print(f"\nanswers      min {min(fits):.4f}  median {sorted(fits)[len(fits) // 2]:.4f}")
    print(f"no answer    max {max(none):.4f}  median {sorted(none)[len(none) // 2]:.4f}")
    print(f"other chats  max {max(others):.4f}")
    print(f"a floor between {max(none):.4f} and {min(fits):.4f} separates them")
    assert min(fits) > max(none), "the reranker tells an answer from none on this set"
    if floor := os.environ.get("CELINE_LIVE_FLOOR"):
        assert max(none) < float(floor) <= min(fits), "the set floor sits between them"
