"""The slop finder (turns/slop.py) and its voice's phrases and patterns
(settings/voice.py, config/her/voice.toml)."""
from __future__ import annotations

import tomllib

import pytest

from celine.her import BASE
from celine.settings import voice
from celine.turns import slop

HERS = voice.validate(tomllib.loads((BASE / "voice.toml").read_text()))


def _what(text):
    return [h["what"] for h in slop.find(text, HERS)]


def test_phrases_match_on_word_boundaries_in_any_case():
    hits = slop.find("Want me to check? I couldn't help but laugh.", HERS)
    assert [(h["what"], h["kind"], h["start"]) for h in hits] == [
        ("want me to", "phrase", 0), ("couldn't help but", "phrase", 20)]
    assert _what("an example of a sample") == [], "ample inside a word is no hit"


def test_patterns_count_her_chatbot_shapes():
    assert len(_what("It's not the pizza, it's you.")) == 1
    assert len(_what("**Short answer:** yes")) == 1
    assert len(_what("(a) the bar\n(b) the couch")) == 1


def test_code_and_quotes_are_someone_elses_words():
    assert _what('he wrote "want me to" and `feel free to`') == []
    assert _what("> want me to\nok") == []
    assert _what("```\nfeel free to\n```") == []


def test_her_words_against_his_give_her_tics():
    hers = ["honestly that is wild", "honestly no", "honestly fine", "ok"]
    theirs = ["ok", "that is fine", "no"]
    top = slop.profile(hers, theirs)
    assert top[0][0] == "honestly" and top[0][1] > 1
    assert all(g != "ok" for g, _ in top), "a word it writes 1 time is not counted"


@pytest.mark.parametrize("text", [
    "your server, your rules, your problem",
    "more speed, more room and more fun",
    "backup ran, notes updated.\nthat's it.",
    "you restart it and done. simple as that",
    "here's the thing: the disk is full",
    "The kicker: it never ran",
    "Honestly, it's fine",
    "ok. honestly i don't care",
    "that's genuinely wild",
    "i caught myself checking twice",
    "to be fair it was late",
])
def test_patterns_count_sales_copy_closers_setups_and_tics(text):
    assert len(_what(text)) == 1, (text, _what(text))


@pytest.mark.parametrize("text", [
    "your server, my rules",                       # two, not three
    "no cheese. no onions. extra sauce.",          # the third breaks the run
    "the cat, the dog, the bird",                  # "the" is a plain list
    "hmph. fine. one bite.",                       # its own short lines
    "He left. It rained. Bad night.",              # short sentences, not one-word ones
    "that's it?",                                  # a question
    "that's it, i'm out of ideas",                 # not the end of the line
    "the thing is the disk",                       # no setup mark
    "the catch is that it's slow",                 # no colon
    "i honestly don't know",                       # not an opener
    "i caught the bus",
    "to be continued",
    "Ok. Fine. Whatever.",                        # its own staccato
    "to be honest-ish it was fine",                # a hyphenated word
    "end of storyline rewrite",
    "your rules, your call, your",                 # nothing after the third
])
def test_patterns_leave_her_plain_lines_alone(text):
    assert _what(text) == [], text


@pytest.mark.parametrize("text", [
    "i fixed it and i'm not gonna be nice about it",
    "cleaned the queue. you're welcome.",
    "you owe me",
    "fight me",
    "and that's that",
    "and i'd do it again",
    "that's me being nice",
    "moved every note into the right folder. period.",
    "the queue is empty, backup is on. it's perfect",
])
def test_patterns_count_toppers(text):
    assert len(_what(text)) == 1, (text, _what(text))


@pytest.mark.parametrize("text", [
    "no 🙃",
    "obviously",
    "ugh FINE",
    "no excuse, that's on me. starting now",
    "you're welcome to join",
    "i can't cope with this",
    "the trial period ends friday",
    "it's perfect for the garden",
    "that's on the list",
    "i'd do it again if the backup fails",
    "fight me for it and you lose",
    "nope",
    "the cat says hi. she bit me.",
    "yeah. that's all. one note with two empty checkboxes",
    "i'll deal with it tomorrow",
    "you owe me 5 bucks",
    "no apologies needed",
    "i'm not sorry for the delay",
    "get back here with the box",
    "and that's that changes nothing",
    "end of story time",
    "deal with it later today",
    "i'm not gonna be nice about it if you ask again",
])
def test_toppers_leave_her_short_lines_alone(text):
    assert _what(text) == [], text


def test_a_button_after_a_long_sentence_is_counted():
    long = "i went through every note in the folder and sorted them by the day they were written. "
    assert len(_what(long + "all clean.")) == 1
    assert _what(long + "all clean, nothing left in scratch.") == []
    assert _what("done. all clean.") == []


def test_a_phrase_in_quotes_or_code_is_not_hers_and_a_pattern_needs_its_context():
    v = {"phrases": ["want me to"], "patterns": [r"\bfriend\b(?=[.,!?])"]}
    got = slop.find('ok friend. Want me to? "want me to" `want me to` friend account', v)
    assert [(h["kind"], h["start"]) for h in got] == [("pattern", 3), ("phrase", 11)], got
    assert slop.profile(["lol lol lol ok"], ["ok ok"])[0][0] == "lol"
