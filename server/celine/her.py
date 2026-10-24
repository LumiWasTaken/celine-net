"""The agent: Celine, the one agent. Its row is the table `her`: name,
pronouns (it, he, they), color, portrait (a blob), persona (who it is: the start of
its system prompt), talk (how it talks, after it; NULL: config/her/talk.md; both kept
with {{name}} and {{username}}, shown and stored through `shown` and `stored`), voice
(its Banned words: the words its turns are steered from, settings/voice.py) and
`named` (its name was saved once, the first run's "Set its name").

The shipped base is `config/her/` (her.toml, persona.md, owner.md, voice.toml,
talk.md): a fresh install's row starts from it (`seed`). `cfg.her` is the live row:
boot.load reads it from its connection (`read_row`), `refresh` through the Store
after every write. Every read and write here goes through the app's Store; nothing
opens a connection of its own. The agent's name and pronouns fill the prompt text
(Config.substitute) through cfg.name and cfg.pronouns, which `apply` sets.
"""
from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, PRONOUNS, Problem
from . import texts
from .core.ids import now

BASE = CONFIG_DIR / "her"
KEYS = {"name", "pronouns", "color"}
COLORS = ("rose", "peach", "sage", "teal", "sky", "violet", "miku")
TEXT_MAX = 200_000         # bytes of a persona or a talk text
MEDIA = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


@dataclass
class Her:
    name: str
    pronouns: str
    color: str
    persona: str                       # {{name}} and {{username}} not filled in
    talk: str | None                   # how it talks, its own text; None: shipped
    voice: dict[str, Any]              # settings/voice.py validate
    notebook: Path                     # the one notebook, var/notebook
    portrait: str | None = None        # a blob's sha256
    updated_at: str = ""
    named: bool = False                # its name was saved once

    @property
    def talk_text(self) -> str:
        return (self.talk if self.talk is not None else shipped_talk()).strip()


def shipped_talk() -> str:
    """How it talks when it has no text of its own (talk.md)."""
    return (BASE / "talk.md").read_text()


def base_persona() -> str:
    """The shipped persona with owner.md after it (its notebook, who it is to the
    user). The agent's shellbox is the shell skill's guide: it reads of it only while it is up."""
    return (BASE / "persona.md").read_text().rstrip() + "\n\n" + (BASE / "owner.md").read_text()


def notebook(cfg) -> Path:
    """The one notebook (skills/notebook.py), var/notebook."""
    return Path(cfg.data_dir) / "notebook"


def _invalid(field_: str, sentence: str):
    from .settings.files import Invalid
    return Invalid(field_, sentence)


# ---- the row ----------------------------------------------------------------------------

def read_row(c):
    """The agent's row (on any connection), None before the first start."""
    return c.execute("SELECT * FROM her WHERE id = 1").fetchone()


def from_row(r, data_dir) -> Her:
    from .settings import voice as voice_mod
    return Her(name=r["name"], pronouns=r["pronouns"], color=r["color"],
               persona=r["persona"], talk=r["talk"],
               voice=voice_mod.validate(json.loads(r["voice"] or "{}")),
               notebook=Path(data_dir) / "notebook", portrait=r["portrait"],
               updated_at=r["updated_at"], named=bool(r["named"]))


def apply(cfg, row, problems: list) -> None:
    """`row` (read_row) into cfg: it, its name and pronouns as cfg's, the names people
    read (intercom/store.py NAMES)."""
    from .settings import compile_her
    if row is not None:
        try:
            cfg.her = from_row(row, cfg.data_dir)
            cfg.name, cfg.pronouns = cfg.her.name, cfg.her.pronouns
        except Exception as e:          # a row a hand edit broke
            problems.append(Problem("her", f"her row not loaded: {e}"))
    compile_her(cfg)


async def refresh(store, cfg) -> None:
    """`cfg.her` again from the row, after a write."""
    apply(cfg, await store.read(read_row), [])


def portrait_view(her: Her | None) -> dict | None:
    """{url}: the url changes with every new portrait; None when it has none."""
    return {"url": f"/api/agent/portrait?v={her.portrait[:12]}"} \
        if her is not None and her.portrait else None


def view(cfg) -> dict[str, Any]:
    """The agent, as every page knows it (GET /api/status `owner`)."""
    h = cfg.her
    return {"name": cfg.name, "pronouns": cfg.pronouns, "color": h.color if h else COLORS[0],
            "portrait": portrait_view(h), "named": bool(h and h.named)}


def shown(cfg, her: Her, text: str | None = None) -> str:
    """The agent's persona (or `text`: its talk) as its page shows it: {{name}} and {{username}}
    as the names."""
    text = her.persona if text is None else text
    for key, value in (("{{name}}", her.name), ("{{username}}", cfg.user)):
        text = text.replace(key, value)
    return text


def package(cfg) -> dict[str, Any]:
    """The whole of it as JSON (GET /api/agent): its persona and talk exactly as typed,
    with any {{name}} and {{username}} in them (filled when a request is built); the
    shipped talk too."""
    h = cfg.her
    return {**view(cfg), "persona": h.persona, "talk": h.talk,
            "talk_shipped": shipped_talk(),
            "voice": h.voice, "voice_text": voice_text(h.voice)}


# ---- checking ----------------------------------------------------------------------------

def parse(raw: dict) -> dict[str, Any]:
    """The agent's plain fields, checked. Raises Invalid with the field and a sentence."""
    unknown = sorted(set(raw) - KEYS)
    if unknown:
        raise _invalid(unknown[0], f"'{unknown[0]}' is not one of the agent's settings.")
    name = check_name(raw.get("name"))
    pronouns = raw.get("pronouns", "she")
    if pronouns not in PRONOUNS:
        raise _invalid("pronouns", f"The pronouns are one of {', '.join(PRONOUNS)}.")
    color = raw.get("color", COLORS[0])
    if color not in COLORS:
        raise _invalid("color", f"The color is one of {', '.join(COLORS)}.")
    return {"name": name, "pronouns": pronouns, "color": color}


def check_name(name: Any) -> str:
    """The agent's name, stripped. Raises Invalid."""
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
        raise _invalid("name", "The name is 1 to 40 characters.")
    if "/" in name or "\\" in name:
        raise _invalid("name", "A name cannot hold / or \\.")
    if not any(ch.isalnum() for ch in name):
        raise _invalid("name", "A name needs at least one letter or digit.")
    return name.strip()


def _text(value: Any, field_: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(field_, "This cannot be empty.")
    if len(value.encode()) > TEXT_MAX:
        raise _invalid(field_, f"This is at most {TEXT_MAX:,} bytes.")
    return value


# ---- writing ----------------------------------------------------------------------------

async def write(store, cfg, body: dict, by: str = "user") -> None:
    """write_sync in a write of its own."""
    await store.write(lambda c: write_sync(c, cfg, body, by))


def write_sync(c, cfg, body: dict, by: str = "user") -> None:
    """Inside a write: any of name, pronouns, color, persona, talk (null: back to the
    shipped one), voice, as its row is now. Everything is checked before anything is
    written; persona, talk and voice (as voice_text) go into their History (texts.py),
    as written by `by`. Raises Invalid."""
    from .settings import voice as voice_mod
    h = from_row(read_row(c), cfg.data_dir)
    for k in body:
        if k not in KEYS | {"persona", "talk", "voice"}:
            raise _invalid(k, f"'{k}' is not one of the agent's settings.")
    values = parse({"name": h.name, "pronouns": h.pronouns, "color": h.color,
                    **{k: body[k] for k in KEYS if k in body}})
    persona = _text(body["persona"], "persona") if "persona" in body else h.persona
    talk = body["talk"] if "talk" in body else h.talk
    if talk is not None:
        talk = _text(talk, "talk")
        if talk.strip() == shipped_talk().strip():
            talk = None
    voice = voice_mod.validate(body["voice"]) if "voice" in body else h.voice
    c.execute("UPDATE her SET name=?, pronouns=?, color=?, persona=?, talk=?, voice=?, "
              "named=?, updated_at=? WHERE id = 1",
              (values["name"], values["pronouns"], values["color"], persona, talk,
               json.dumps(voice), int(h.named or "name" in body), now()))
    for field, was, text in (("persona", h.persona, persona),
                             ("talk", h.talk_text, talk if talk is not None else
                              shipped_talk().strip()),
                             ("voice", voice_text(h.voice), voice_text(voice))):
        if field in body:
            texts.keep(c, f"her:{field}", was, text, by)


# ---- its voice as text (History of her:voice) -------------------------------------------

_ONE = {"banned": "banned", "phrases": "phrase", "patterns": "pattern"}


def voice_text(voice: dict) -> str:
    """The agent's voice (settings/voice.py) as text, one line each: `on: true`, `bias: -8.0`,
    then `banned: <word>` (its leading space kept), `phrase: ...`, `pattern: ...`. The
    form History keeps (her:voice) and voice_from_text reads back."""
    lines = [f"on: {'true' if voice.get('on', True) else 'false'}",
             f"bias: {float(voice.get('bias', -8.0))}"]
    for key, word in _ONE.items():
        lines += [f"{word}: {x}" for x in voice.get(key) or []]
    return "\n".join(lines)


def voice_from_text(text: str) -> dict:
    """voice_text's other way, checked (settings/voice.py). Raises Invalid."""
    from .settings import voice as voice_mod
    out: dict = {"banned": [], "phrases": [], "patterns": []}
    by_word = {w: k for k, w in _ONE.items()}
    for n, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        key, sep, value = line.partition(": ")
        if not sep or key not in (*by_word, "on", "bias"):
            raise _invalid("voice", f"Line {n} is not one of on, bias, banned, phrase, "
                                    f"pattern.")
        if key == "on":
            out["on"] = value.strip() == "true"
        elif key == "bias":
            try:
                out["bias"] = float(value)
            except ValueError:
                raise _invalid("voice", f"Line {n}: the bias is a number.") from None
        else:
            out[by_word[key]].append(value)
    return voice_mod.validate(out)


# ---- portraits ---------------------------------------------------------------------------

def image_kind(data: bytes) -> str | None:
    """png, jpg or webp from the first bytes; None for anything else."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _blob(cfg, c, data: bytes) -> str:
    """`data` as a blob (attachments.py's store of files by hash). -> its sha256.
    ValueError when it is no png, jpg or webp."""
    from .attachments import blob_path
    kind = image_kind(data)
    if kind is None:
        raise ValueError("The portrait is a PNG, JPEG or WebP image.")
    sha = sha256(data).hexdigest()
    p = blob_path(cfg, sha)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{sha}.tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
    c.execute("INSERT OR IGNORE INTO blobs(sha256, bytes, media_type, created_at) "
              "VALUES(?,?,?,?)", (sha, len(data), MEDIA[kind], now()))
    return sha


async def set_portrait(store, cfg, data: bytes | None) -> None:
    """The agent's portrait from the image's bytes (None: no portrait). ValueError when it is
    no png, jpg or webp."""
    if data is not None and image_kind(data) is None:
        raise ValueError("The portrait is a PNG, JPEG or WebP image.")

    def fn(c) -> None:
        sha = _blob(cfg, c, data) if data is not None else None
        c.execute("UPDATE her SET portrait=?, updated_at=? WHERE id = 1", (sha, now()))
    await store.write(fn)


def portrait_file(cfg) -> tuple[Path, str] | None:
    """(the file, its media type), None when it has none."""
    h = cfg.her
    if h is None or not h.portrait:
        return None
    from .attachments import blob_path
    p = blob_path(cfg, h.portrait)
    kind = image_kind(p.read_bytes()[:16]) if p.is_file() else None
    return (p, MEDIA[kind]) if kind else None


# ---- the first start ----------------------------------------------------------------

def seed(c) -> bool:
    """A fresh install's row from the shipped base, when there is none. -> made."""
    if read_row(c) is not None:
        return False
    raw = tomllib.loads((BASE / "her.toml").read_text())
    voice = tomllib.loads((BASE / "voice.toml").read_text())
    values = parse({k: raw[k] for k in ("name", "pronouns", "color")})
    c.execute("INSERT INTO her(id, name, pronouns, color, persona, talk, voice, named, "
              "updated_at) VALUES(1,?,?,?,?,NULL,?,0,?)",
              (values["name"], values["pronouns"], values["color"], base_persona(),
               json.dumps(voice), now()))
    return True


REVERTS = ("persona", "talk", "voice")


async def revert(services, vid: int) -> dict:
    """Version `vid` of its persona, talk or voice (texts.py) becomes the text again, as a
    new version written by the user (voice from its text form, voice_from_text).
    -> {of, text}. Another kind of text is restored in its editor (Restore this version)
    and saved there: 400."""
    from . import texts
    from .core.event_types import HER_CHANGED
    from .errors import CelineError, NotFound
    from .settings import write as settings_write
    store, cfg = services.store, services.cfg
    row = await store.read(lambda c: texts.raw(c, vid))
    if row is None:
        raise NotFound("version")
    field = row["of"].removeprefix("her:")
    if field not in REVERTS:
        raise CelineError("bad_request", "Only the persona, talk and voice revert here.", 400)
    value = voice_from_text(row["text"]) if field == "voice" else row["text"]
    async with settings_write.lock():
        await write(store, cfg, {field: value})
        await refresh(store, cfg)
    store.emit_global({"type": HER_CHANGED})
    return {"of": row["of"], "text": row["text"]}
