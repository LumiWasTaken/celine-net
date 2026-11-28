"""Candidate passages for the character-card genres: real cards of one tag from the card
database, most starred first, whole (a card whose text is over the size is
left out, never cut). Writes genres/local/<genre>.candidates.json; the labelled genre file
beside it is written by hand from these.

    python3 cards.py <genre> <tag> [<tag> ...]      a card carries nsfw and one of the tags
    python3 cards.py haystack <count>               the most starred nsfw cards of any tag:
                                                    what the embedders search through
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).parent / "genres" / "local"
# Left out: a character who is a minor or reads as one, and force.
TAGS = ("loli", "shota", "teenager", "underage", "child", "schoolgirl", "schoolboy", "young",
        "student", "school", "high school", "kid", "teen", "daughter", "son", "rape",
        "possible rape", "non-con", "noncon", "nsfl", "incest", "family", "petite", "small")
TEXT = (r"\m(1[0-7]|[1-9])\M[ -]*(years?|yrs?|y/?o)|\mage:?\s*\[?(1[0-7]|[1-9])\M|loli|shota|"
        r"child|\mkids?\M|teen|school|student|underage|\mminor|\myoung|daughter|\mgirl\M|"
        r"\mboy\M|pupil|puberty|adolescen|\mrape|non-?con")
SQL = """
select json_build_object('name', name, 'author', author, 'tags', tags, 'tagline', tagline,
                         'text', descr, 'stars', stars)
from (select distinct on (d.id) d.name, d.author, d.search_tags as tags,
             c.data->>'tagline' as tagline, d.definition->'data'->>'description' as descr,
             coalesce((c.data->>'starCount')::int, 0) as stars
      from chub_character_def d join chub_character c on c.id = d.id
      where d.search_tags @> array['nsfw'] {tags}
        and not d.search_tags && array[{bad}]
      order by d.id, d.increment desc) s
where length(descr) between 500 and 1400 and descr !~* '{text}' and name !~* '{text}'
  and coalesce(tagline, '') !~* '{text}' and descr !~ '[^\\x00-\\x7f]{{4}}'
order by stars desc limit {limit};
"""


def lit(xs) -> str:
    return ", ".join("'" + x.replace("'", "''") + "'" for x in xs)


def main(genre: str, tags: list[str]) -> None:
    hay = genre == "haystack"
    sql = SQL.format(tags="" if hay else f"and d.search_tags && array[{lit(tags)}]",
                     bad=lit(TAGS), text=TEXT, limit=int(tags[0]) if hay else 40)
    # CARDS_PSQL: the command that reads SQL on stdin and prints unaligned tuples (psql -At)
    out = subprocess.run(shlex.split(os.environ["CARDS_PSQL"]), input=sql, capture_output=True,
                         text=True, check=True).stdout
    seen, passages = set(), []
    for c in map(json.loads, filter(None, out.split("\n"))):
        if c["name"].casefold() in seen:
            continue
        seen.add(c["name"].casefold())
        passages.append(f"Character card: {c['name']} (by {c['author']})\n"
                        f"Tags: {', '.join(c['tags'])}\n{(c['tagline'] or '').strip()}\n"
                        f"{c['text'].strip()}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / ("haystack.json" if hay else f"{genre}.candidates.json")).write_text(
        json.dumps(passages, ensure_ascii=False, indent=1))
    print(genre, len(passages), "candidates,", sum(map(len, passages)) // len(passages), "chars each")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
