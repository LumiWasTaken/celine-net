"""Presets: what a ComfyUI workflow is for, and how a request fills it.

One folder per kind: config/image_presets/ (images) and config/music_presets/ (music)
ship them; var/settings/image_presets/ and music_presets/ hold the user's (Settings,
Images & Music): an edited shipped preset under its own id (Reset to defaults deletes it,
the shipped one is back), and the user's own uploads. A preset is a pair of files with
one name: `<id>.toml` describes it for people and for Celine, `<id>.json` is the ComfyUI
workflow in API format (ComfyUI's "Export (API)"). The workflow's own values are the
defaults.

The TOML:
- label; family (the model family), good_at (one or two sentences), prompting (how to
  write for it), example (a prompt or a style), each optional; workflow (the JSON file
  beside it);
- task: images only, "generate" (a new image from text, the default) or "edit" (an
  image changed by an instruction);
- [parameters.<name>]: the inputs the app fills, `inputs = [[node, input, key...], ...]`
  (a key reaches into an input that is an object). Optional: `choices` (the word Celine
  gives -> the node's value). The names of each kind are PARAMETERS;
- size: images only, {step, max_pixels} (default 32 and 4.3 megapixels);
- [catalog]: images only, which models and LoRAs of the server are this preset's own
  (image_catalog.py): models and loras (name prefixes), lora_after (the node a LoRA
  chain follows), sampler (the node a model's settings go to), text ([node, input] that
  trigger words are put in front of), max_loras, full_settings (the sampler settings of
  a model that is not turbo).

`load(kind)` reads both folders once per change of their files and checks every preset:
each problem is one sentence for the configuration banner (config.Problem), and a preset
with a problem is left out.

An upload (`upload`) is a workflow in API format whose inputs the app fills hold
placeholders: the whole value of an input is "{{prompt}}", or "{{width:1024}}" with its
default. Each placeholder becomes the parameter of that name, its places the inputs it
fills, its default the workflow's value there. A download (`with_placeholders`) gives the
workflow back the same way.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import CONFIG_DIR, ROOT, Problem
from ..errors import CelineError

FOLDERS = {"images": "image_presets", "music": "music_presets"}
ROLES = {"images": "image_generation", "music": "music_generation"}
TASKS = ("generate", "edit")
STEP = 32                           # width and height are multiples of this, unless a preset says
SIDE_MIN, SIDE_MAX, PIXELS_MAX = 256, 2752, 4_300_000

# every parameter a preset may take: (type, label, what it is)
PARAMETERS: dict[str, dict[str, tuple[str, str, str]]] = {
    "images": {
        "prompt": ("text", "Prompt", "What to draw, or for an edit what to change."),
        "negative": ("text", "Negative prompt", "What to keep out of the image."),
        "seed": ("integer", "Seed", "The noise the image starts from: the same seed and "
                                    "prompt give the same image."),
        "width": ("integer", "Width", "In pixels."),
        "height": ("integer", "Height", "In pixels."),
        "model": ("text", "Model", "A model of the preset's catalog."),
        "image": ("image", "Source image", "The image an edit starts from."),
        "image2": ("image", "Second image", "Another image an edit takes (a second "
                                            "person, an outfit); its node goes when there "
                                            "is none, as for the third and fourth."),
        "image3": ("image", "Third image", "As the second image."),
        "image4": ("image", "Fourth image", "As the second image."),
        "mode": ("choice", "Variant", "A way the preset runs: its variants."),
    },
    "music": {
        "style": ("text", "Style", "Genre, instruments, tempo, space and mood."),
        "lyrics": ("text", "Lyrics", "With section tags like [Verse 1] and [Chorus]."),
        "seed": ("integer", "Seed", "The same seed and style give the same track."),
        "duration": ("number", "Length", "The most the track may last, in seconds."),
        "instrumental": ("boolean", "Instrumental", "No vocals."),
    },
}
# an edit's images: the parameter each fills and the field of its item it comes from
IMAGES = ("image", "image2", "image3", "image4")
SOURCES = ("source", "source2", "source3", "source4")
REQUIRED = {"generate": ("prompt", "seed", "width", "height"),
            "edit": ("prompt", "seed", "width", "height", "image"),
            "music": ("style", "seed")}


class ImageError(Exception):
    """Something made or asked for that did not happen. The message is one or two
    sentences for Celine and the user."""


@dataclass(frozen=True)
class Param:
    inputs: tuple[tuple[str, ...], ...]
    choices: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Preset:
    id: str
    kind: str                       # images | music
    label: str
    family: str
    good_at: str
    prompting: str
    example: str
    workflow: dict[str, Any]
    parameters: dict[str, Param]
    task: str = "generate"          # generate | edit | music
    origin: str = "shipped"         # shipped | edited (a shipped one the user changed) | own
    source: dict[str, Any] = field(default_factory=dict)    # the TOML as read
    size_step: int = STEP
    max_pixels: int = PIXELS_MAX
    # the catalog (image_catalog.py)
    models: tuple[str, ...] = ()
    loras: tuple[str, ...] = ()
    lora_after: str | None = None
    sampler: str | None = None
    text: tuple[str, str] | None = None
    max_loras: int = 0
    full_settings: dict[str, Any] = field(default_factory=dict)

    @property
    def role(self) -> str:
        return ROLES[self.kind]

    def default(self, name: str) -> Any:
        """The workflow's value of a parameter (for a choice, its word)."""
        value = _get(self.workflow, self.parameters[name].inputs[0])
        words = {v: k for k, v in self.parameters[name].choices.items()}
        return words.get(value, value)

    @property
    def size(self) -> tuple[int, int]:
        return int(self.default("width")), int(self.default("height"))

    def takes(self, name: str) -> bool:
        return name in self.parameters

    @property
    def images(self) -> int:
        """How many images an edit of this preset takes."""
        return sum(name in self.parameters for name in IMAGES)

    def build(self, values: dict[str, Any]) -> dict[str, Any]:
        """The workflow with `values` in the inputs their parameters name; a choice
        word becomes its node value."""
        graph = copy.deepcopy(self.workflow)
        for name, value in values.items():
            param = self.parameters.get(name)
            if param is None:
                continue
            value = param.choices.get(value, value) if param.choices else value
            for path in param.inputs:
                _set(graph, path, value)
        return graph

    def missing(self, info: dict[str, Any]) -> str | None:
        """Why a server cannot run this preset, from its `/object_info` entries
        ({class: entry or None}); None when it can. The source image of an edit is
        uploaded per call, so its input is not checked."""
        filled = {p[:2] for name in IMAGES if name in self.parameters
                  for p in self.parameters[name].inputs}
        for nid, node in self.workflow.items():
            cls = node["class_type"]
            entry = info.get(cls)
            if not entry:
                return (f"The preset {self.label} needs the node {cls}, which ComfyUI does "
                        f"not have.")
            spec = {**(entry.get("input") or {}).get("required", {}),
                    **(entry.get("input") or {}).get("optional", {})}
            for key, value in node["inputs"].items():
                if (nid, key) in filled:
                    continue
                choices = options(spec.get(key))
                if choices is not None and isinstance(value, str) and value not in choices:
                    return (f"The preset {self.label} needs {value} ({cls}), which ComfyUI "
                            f"does not have.")
        return None

    def fit(self, width: int | None, height: int | None,
            source: tuple[int, int] | None = None) -> tuple[int, int]:
        """Width and height: the ones given, else the source image's shape at the
        preset's pixel count (an edit), else the preset's own size. Multiples of the
        preset's step, within the limits."""
        step = self.size_step
        dw, dh = self.size
        if source and not (width or height):
            a, b = source
            dw, dh = math.sqrt(dw * dh * a / b), math.sqrt(dw * dh * b / a)
        w, h = (max(step, round(float(v) / step) * step)
                for v in (width or dw, height or dh))
        if not (SIDE_MIN <= w <= SIDE_MAX and SIDE_MIN <= h <= SIDE_MAX) or \
                w * h > self.max_pixels:
            raise ImageError(f"{width or w}x{height or h} is outside the limits of "
                             f"{self.label}. Each side is "
                             f"{SIDE_MIN} to {SIDE_MAX} pixels, and the image is at most "
                             f"{self.max_pixels / 1e6:.1f} megapixels.")
        return w, h


def unwire(graph: dict[str, Any], node: str) -> dict[str, Any]:
    """`graph` without `node` and without every input that takes from it (a second
    image that is not given)."""
    out = {k: v for k, v in graph.items() if k != node}
    for n in out.values():
        n["inputs"] = {k: v for k, v in n["inputs"].items()
                       if not (isinstance(v, list) and v and v[0] == node)}
    return out


def _get(graph: dict, path: tuple[str, ...]) -> Any:
    v: Any = graph[path[0]]["inputs"]
    for key in path[1:]:
        v = v[key]
    return v


def _set(graph: dict, path: tuple[str, ...], value: Any) -> None:
    v: Any = graph[path[0]]["inputs"]
    for key in path[1:-1]:
        v = v[key]
    v[path[-1]] = value


def options(spec: Any) -> list | None:
    """The choices of a combo input in /object_info: the old form [[...], {...}] or
    the new form ["COMBO", {"options": [...]}]."""
    if not isinstance(spec, list) or not spec:
        return None
    if isinstance(spec[0], list):
        return spec[0]
    if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        return list(spec[1].get("options") or [])
    return None


# ---- loading -------------------------------------------------------------------------

_cache: dict[Path, tuple[tuple, dict[str, Preset], list[Problem]]] = {}


def folder(kind: str) -> Path:
    return CONFIG_DIR / FOLDERS[kind]


def user_folder(kind: str) -> Path:
    """var/settings/<folder>: the user's presets, in the data dir config.load names."""
    data = os.environ.get("CELINE_DATA_DIR")
    if data:
        root = Path(data).expanduser()
    else:
        try:
            main = tomllib.loads((CONFIG_DIR / "celine.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            main = {}
        root = Path((main.get("paths") or {}).get("data", "var"))
    if not root.is_absolute():
        root = ROOT / root
    return root / "settings" / FOLDERS[kind]


def load(kind: str) -> tuple[dict[str, Preset], list[Problem]]:
    """Every valid preset of `kind` by id: the shipped ones in file order, each replaced
    by the user's edit of it, then the user's own. A Problem per sentence of what is
    wrong."""
    shipped, problems = load_dir(kind, folder(kind))
    user, theirs = load_dir(kind, user_folder(kind),
                            f"var/settings/{FOLDERS[kind]}", origin="own")
    found = dict(shipped)
    for pid, p in user.items():
        found[pid] = _with(p, origin="edited") if pid in shipped else p
    return found, problems + theirs


def _with(p: Preset, **changes) -> Preset:
    from dataclasses import replace
    return replace(p, **changes)


def load_dir(kind: str, root: Path, rel: str | None = None, origin: str = "shipped"
             ) -> tuple[dict[str, Preset], list[Problem]]:
    """The valid presets of one folder, read again only when a file of it changed."""
    files = sorted(root.iterdir()) if root.is_dir() else []
    key = tuple((p.name, p.stat().st_mtime_ns) for p in files)
    hit = _cache.get(root)
    if hit is not None and hit[0] == key:
        return hit[1], hit[2]
    found: dict[str, Preset] = {}
    problems: list[Problem] = []
    for path in (p for p in files if p.suffix == ".toml"):
        bad: list[str] = []
        preset = _read(kind, path, bad, origin)
        problems += [Problem(f"{rel or 'config/' + root.name}/{path.name}", b) for b in bad]
        if preset is not None and not bad:
            found[preset.id] = preset
    _cache[root] = (key, found, problems)
    return found, problems


def of(kind: str) -> dict[str, Preset]:
    return load(kind)[0]


def kind_of(preset_id: str) -> str | None:
    """images or music, for a ComfyUI model id; None for a preset that is not there."""
    return next((k for k in FOLDERS if preset_id in of(k)), None)


def _read(kind: str, path: Path, bad: list[str], origin: str) -> Preset | None:
    """One preset file and the workflow beside it, with every problem appended to `bad`
    as a sentence."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        bad.append(f"Not readable: {e}.")
        return None
    wf_name = data.get("workflow")
    if not isinstance(wf_name, str) or not wf_name.strip():
        bad.append("workflow is missing: it is a text.")
        return None
    try:
        workflow = json.loads((path.parent / wf_name.strip()).read_text(encoding="utf-8"))
    except OSError:
        bad.append(f"The workflow {wf_name} is not beside it.")
        return None
    except ValueError as e:
        bad.append(f"The workflow {wf_name} is not JSON: {e}.")
        return None
    if not _api_format(workflow):
        bad.append(f"The workflow {wf_name} is not in ComfyUI's API format: "
                   f"{{id: {{class_type, inputs}}}}.")
        return None
    return build(kind, path.stem, data, workflow, bad, origin)


def _api_format(workflow: Any) -> bool:
    return isinstance(workflow, dict) and bool(workflow) and all(
        isinstance(n, dict) and isinstance(n.get("class_type"), str)
        and isinstance(n.get("inputs"), dict) for n in workflow.values())


def build(kind: str, pid: str, data: dict[str, Any], workflow: dict[str, Any],
          bad: list[str], origin: str = "shipped") -> Preset | None:
    """A preset from its TOML (`data`) and its workflow, checked; every problem is
    appended to `bad` as a sentence and makes it None."""
    def text(key: str, required: bool = False) -> str:
        v = data.get(key, "")
        if not isinstance(v, str) or (required and not v.strip()):
            bad.append(f"{key} is missing: it is a text." if required else f"{key} is a text.")
            return ""
        return v.strip()

    known = {"label", "family", "good_at", "prompting", "example", "workflow", "task",
             "parameters", "size", "catalog"}
    for k in data:
        if k not in known:
            bad.append(f"{k} is not a preset setting.")
    label = text("label", required=True)
    family, good_at = text("family"), text("good_at")
    prompting, example = text("prompting"), text("example")
    task = "music"
    if kind == "images":
        task = data.get("task", "generate")
        if task not in TASKS:
            bad.append(f"task is {' or '.join(TASKS)}.")
            return None
    elif "task" in data:
        bad.append("task is for image presets only.")

    params: dict[str, Param] = {}
    raw = data.get("parameters") or {}
    for name, spec in raw.items() if isinstance(raw, dict) else ():
        if name not in PARAMETERS[kind]:
            bad.append(f"parameters.{name} is not a parameter of {kind} presets. They are: "
                       f"{', '.join(PARAMETERS[kind])}.")
            continue
        inputs = spec.get("inputs") if isinstance(spec, dict) else None
        if not isinstance(inputs, list) or not inputs or not all(
                isinstance(i, list) and len(i) >= 2 for i in inputs):
            bad.append(f"parameters.{name}.inputs is a list of [node, input].")
            continue
        paths = tuple(tuple(str(x) for x in i) for i in inputs)
        for p in paths:
            try:
                _get(workflow, p)
            except (KeyError, TypeError):
                bad.append(f"parameters.{name}: the workflow has no input "
                           f"{' > '.join(p)}.")
        choices = spec.get("choices") or {}
        if not isinstance(choices, dict):
            bad.append(f"parameters.{name}.choices maps each word to the node's value.")
            choices = {}
        params[name] = Param(inputs=paths, choices=dict(choices))
    for name in REQUIRED[task]:
        if name not in params:
            bad.append(f"parameters.{name} is missing.")
    if bad:
        return None
    for name, p in params.items():
        if p.choices and _get(workflow, p.inputs[0]) not in p.choices.values():
            bad.append(f"parameters.{name}: the workflow's value is none of its choices.")

    size = data.get("size") or {}
    if not isinstance(size, dict) or set(size) - {"step", "max_pixels"} or not all(
            isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in size.values()):
        bad.append("size has step and max_pixels, whole numbers above 0.")
        size = {}

    cat = data.get("catalog") or {}
    if not isinstance(cat, dict):
        bad.append("catalog is a table.")
        cat = {}
    for k in cat:
        if k not in ("models", "loras", "lora_after", "sampler", "text", "max_loras",
                     "full_settings"):
            bad.append(f"catalog.{k} is not a catalog setting.")
    models = tuple(str(x) for x in cat.get("models") or ())
    loras = tuple(str(x) for x in cat.get("loras") or ())
    lora_after = str(cat["lora_after"]) if cat.get("lora_after") else None
    sampler = str(cat["sampler"]) if cat.get("sampler") else None
    text_in = cat.get("text")
    for node, what in ((lora_after, "lora_after"), (sampler, "sampler")):
        if node is not None and node not in workflow:
            bad.append(f"catalog.{what}: the workflow has no node {node}.")
    if text_in is not None:
        if not (isinstance(text_in, list) and len(text_in) == 2):
            bad.append("catalog.text is [node, input].")
            text_in = None
        else:
            text_in = (str(text_in[0]), str(text_in[1]))
            if text_in[1] not in workflow.get(text_in[0], {}).get("inputs", {}):
                bad.append(f"catalog.text: the workflow has no input {' > '.join(text_in)}.")
    if models and "model" not in params:
        bad.append("catalog.models needs the parameter model.")
    if loras and not lora_after:
        bad.append("catalog.loras needs catalog.lora_after.")
    max_loras = cat.get("max_loras", 4 if loras else 0)
    if not isinstance(max_loras, int) or isinstance(max_loras, bool) or max_loras < 0:
        bad.append("catalog.max_loras is a whole number.")
    full = cat.get("full_settings") or {}
    if not isinstance(full, dict) or set(full) - set(_SAMPLER_KEYS):
        bad.append(f"catalog.full_settings has {', '.join(dict.fromkeys(_SAMPLER_KEYS))}.")
    if bad:
        return None
    return Preset(
        id=pid, kind=kind, label=label, family=family, good_at=good_at,
        prompting=prompting, example=example, workflow=workflow, parameters=params,
        task=task, origin=origin, source=copy.deepcopy(data),
        size_step=int(size.get("step", STEP)),
        max_pixels=int(size.get("max_pixels", PIXELS_MAX)), models=models, loras=loras,
        lora_after=lora_after, sampler=sampler, text=text_in, max_loras=max_loras,
        full_settings=dict(full))


# ---- for people and for it ------------------------------------------------------------

def view(p: Preset) -> dict[str, Any]:
    """A preset as the UI shows it: everything but the workflow."""
    params = []
    for name, spec in p.parameters.items():
        if name in IMAGES:
            continue
        kind, label, about = PARAMETERS[p.kind][name]
        entry: dict[str, Any] = {"name": name, "type": kind, "label": label,
                                 "description": about}
        if name not in ("prompt", "style", "seed"):
            entry["default"] = p.default(name)
        if spec.choices:
            entry["choices"] = list(spec.choices)
        if name in ("width", "height"):
            entry.update(min=SIDE_MIN, max=SIDE_MAX, step=p.size_step)
        params.append(entry)
    out: dict[str, Any] = {
        "id": p.id, "kind": p.kind, "label": p.label, "family": p.family, "task": p.task,
        "good_at": p.good_at, "prompting": p.prompting, "example": p.example,
        "parameters": params}
    if p.kind == "images":
        w, h = p.size
        out["size"] = {"width": w, "height": h, "step": p.size_step,
                       "max_pixels": p.max_pixels}
        out["negative"] = p.takes("negative")
        out["images"] = p.images
        out["catalog"] = {"models": bool(p.models), "max_loras": p.max_loras} \
            if p.models or p.loras else None
    out.update(origin=p.origin, fields=fields(p), values=values(p))
    return out


def docs(p: Preset, first: bool) -> str:
    """One preset in its guide: what it is for, how it takes a prompt, an example."""
    what = {"generate": "new images", "edit": "edits", "music": "music"}[p.task]
    family = f" ({p.family})" if p.family else ""
    head = f"### `{p.id}`: {p.label}{family}, {what}{', the default' if first else ''}"
    lines = [head] + ([p.good_at] if p.good_at else [])
    facts = []
    if p.kind == "images":
        w, h = p.size
        facts.append(f"Size {w}x{h}, sides in steps of {p.size_step}, at most "
                     f"{p.max_pixels / 1e6:.1f} megapixels.")
        facts.append("Reads a negative prompt." if p.takes("negative")
                     else "Reads no negative prompt.")
        if p.images > 1:
            names = [f"`{x}`" for x in SOURCES[:p.images]]
            facts.append(f"Takes up to {p.images} images: {', '.join(names[:-1])} and "
                         f"{names[-1]}.")
        if "mode" in p.parameters:
            facts.append(f"Variants (the `variant` field): "
                         f"{', '.join(p.parameters['mode'].choices)} (default "
                         f"{p.default('mode')}).")
        if p.models or p.loras:
            facts.append("Takes a model" + (f" and up to {p.max_loras} LoRAs" if p.loras
                                           else "") + " (list_presets lists them).")
    else:
        if "duration" in p.parameters:
            facts.append(f"Duration: {p.default('duration'):g} s by default.")
    if facts:
        lines.append(" ".join(facts))
    if p.prompting:
        lines += ["", p.prompting]
    if p.example:
        lines += ["", "Example:", "```", p.example, "```"]
    return "\n".join(lines)


# ---- uploads and downloads: placeholders ----------------------------------------------

class PresetError(CelineError):
    """A preset or an upload the app refuses: 400 with every reason, one sentence each."""

    def __init__(self, problems: list[str]):
        super().__init__("bad_request", " ".join(problems), 400)
        self.problems = problems


_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*(?::(.*?))?\s*\}\}", re.S)
# what the runtime fills on every run: a default would never be used
ALWAYS = ("prompt", "style", "seed", *IMAGES)
# a default the runtime reads: the size, the length, the variant, the model file
NEEDS_DEFAULT = ("width", "height", "duration", "mode", "model")
NOUN = {"images": "image", "music": "music"}
EXPORT = ("In ComfyUI, save it with Workflow, Export (API), and upload that file.")


def ph(name: str) -> str:
    return "{{" + name + "}}"


def parse_workflow(text: str) -> dict[str, Any]:
    """An uploaded file as a workflow in API format; PresetError says why it is not."""
    try:
        wf = json.loads(text)
    except ValueError as e:
        raise PresetError([f"The file is not JSON: {e}."]) from None
    if isinstance(wf, dict) and isinstance(wf.get("nodes"), list):
        raise PresetError([f"This is the workflow as ComfyUI draws it, not its API "
                           f"format. {EXPORT}"])
    if not _api_format(wf):
        raise PresetError([f"The file is not a ComfyUI workflow in API format. {EXPORT}"])
    return wf


def _typed(kind: str, name: str, raw: str | None, bad: list[str]) -> Any:
    """A placeholder's default as its parameter takes it."""
    typ = PARAMETERS[kind][name][0]
    if raw is None:
        return {"text": "", "integer": 0, "number": 0, "boolean": False}.get(typ, "")
    if typ in ("text", "image"):
        return raw
    try:
        v = json.loads(raw)
    except ValueError:
        v = raw
    if typ == "integer" and not (isinstance(v, int) and not isinstance(v, bool)):
        bad.append(f"{ph(name + ':' + raw)}: the default is a whole number.")
    elif typ == "number" and (isinstance(v, bool) or not isinstance(v, (int, float))):
        bad.append(f"{ph(name + ':' + raw)}: the default is a number.")
    elif typ == "boolean" and not isinstance(v, bool):
        bad.append(f"{ph(name + ':' + raw)}: the default is true or false.")
    return v


def placeholders(kind: str, wf: dict[str, Any]
                 ) -> tuple[dict[str, Any], dict[str, list[list[str]]], str]:
    """The workflow with each placeholder replaced by its default, the inputs each
    parameter fills ({name: [[node, input, key...]]}) and the task (an image placeholder
    makes an edit). PresetError names every placeholder that is missing, unknown, inside
    other text or without the default it needs."""
    graph = copy.deepcopy(wf)
    found: dict[str, list[list[str]]] = {}
    given: dict[str, str | None] = {}
    bad: list[str] = []

    def walk(obj: dict, path: list[str]) -> None:
        for key, value in obj.items():
            here = path + [str(key)]
            if isinstance(value, dict):
                walk(value, here)
            elif isinstance(value, str) and "{{" in value:
                m = _PLACEHOLDER.fullmatch(value.strip())
                if m is None:
                    bad.append(f"{' > '.join(here)} has a placeholder inside other text: "
                               f"a placeholder is the whole value, like {ph('prompt')}.")
                    continue
                name, default = m.group(1), m.group(2)
                if name not in PARAMETERS[kind]:
                    bad.append(f"{ph(name)} is not a placeholder of {NOUN[kind]} presets. They "
                               f"are: {', '.join(ph(n) for n in PARAMETERS[kind])}.")
                    continue
                found.setdefault(name, []).append(here)
                if given.get(name) is None:
                    given[name] = default.strip() if default is not None else None

    for nid, node in graph.items():
        walk(node["inputs"], [str(nid)])
    task = "music" if kind == "music" else (
        "edit" if any(n in found for n in IMAGES) else "generate")
    for name in REQUIRED[task]:
        if name not in found:
            what = PARAMETERS[kind][name][1].lower()
            bad.append(f"{ph(name)} is missing: put it as the value of the input that "
                       f"takes the {what}.")
    for name in found:
        if name in NEEDS_DEFAULT and given.get(name) is None:
            bad.append(f"{ph(name)} needs its default, like {ph(name + ':1024')}."
                       if name in ("width", "height") else
                       f"{ph(name)} needs its default, like {ph(name + ':value')}.")
    values = {n: _typed(kind, n, given.get(n), bad) for n in found}
    if bad:
        raise PresetError(list(dict.fromkeys(bad)))
    for name, paths in found.items():
        for path in paths:
            _set(graph, tuple(path), values[name])
    return graph, found, task


def with_placeholders(p: Preset) -> dict[str, Any]:
    """The workflow as an upload takes it: every input the app fills is its placeholder,
    with the workflow's value as its default where the runtime reads one."""
    graph = copy.deepcopy(p.workflow)
    for name, param in p.parameters.items():
        value = _get(p.workflow, param.inputs[0])
        if name in ALWAYS:
            text = ph(name)
        else:
            raw = value if isinstance(value, str) else json.dumps(value)
            text = ph(f"{name}:{raw}") if raw != "" else ph(name)
        for path in param.inputs:
            _set(graph, path, text)
    return graph


# ---- the editor (Settings, Images & Music) ----------------------------------------------

TEXTS = {"label": "Name", "family": "Family", "good_at": "Good at",
         "prompting": "Prompting", "example": "Example"}
LONG = {"good_at", "prompting", "example", "default.lyrics", "default.negative", "variants"}


def _defaulted(p: Preset) -> list[str]:
    return [n for n in p.parameters if n not in ALWAYS]


def fields(p: Preset) -> list[dict[str, Any]]:
    """The editor's fields (web: SchemaForm), each saved on its own change."""
    out: list[dict[str, Any]] = [
        {"key": k, "label": label, "type": "text", "long": k in LONG}
        for k, label in TEXTS.items()]
    for name in _defaulted(p):
        typ, label, about = PARAMETERS[p.kind][name]
        f: dict[str, Any] = {"key": f"default.{name}", "label": label, "help": about,
                             "long": f"default.{name}" in LONG}
        if typ == "boolean":
            f["type"] = "bool"
        elif typ == "choice" and p.parameters[name].choices:
            f.update(type="choice", choices=[[w, w] for w in p.parameters[name].choices])
        elif typ in ("integer", "number"):
            f.update(type="number", unit="seconds" if name == "duration" else "pixels")
        else:
            f["type"] = "text"
        out.append(f)
    if "mode" in p.parameters:
        out.append({"key": "variants", "label": "Variants", "type": "text", "long": True,
                    "help": "One a line: its word = the value the node takes."})
    if p.kind == "images":
        out += [
            {"key": "size.step", "label": "Size step", "type": "number", "unit": "pixels",
             "min": 1},
            {"key": "size.max_pixels", "label": "Largest image", "type": "number",
             "unit": "megapixels", "min": 0.1},
            {"key": "catalog.models", "label": "Models", "type": "text",
             "help": "The model names it takes, by their start (krea2/), comma separated."},
            {"key": "catalog.loras", "label": "LoRAs", "type": "text",
             "help": "The LoRA names it takes, by their start, comma separated."},
            {"key": "catalog.max_loras", "label": "LoRAs per image", "type": "number",
             "min": 0},
            {"key": "catalog.lora_after", "label": "LoRA chain after", "type": "text",
             "help": "The id of the node the LoRAs follow."},
            {"key": "catalog.sampler", "label": "Sampler node", "type": "text",
             "help": "The id of the node a model's steps and CFG go to."},
            {"key": "catalog.text", "label": "Trigger words node", "type": "text",
             "help": "Node id, input: where a LoRA's trigger words go in front."}]
        out += [{"key": f"catalog.full.{k}", "label": f"Full model {label}", "type": "text",
                 "help": "For a model that is not turbo."}
                for k, label in (("steps", "steps"), ("cfg", "CFG"), ("sampler", "sampler"),
                                 ("scheduler", "scheduler"))]
    return out


def _word(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v)


def values(p: Preset) -> dict[str, Any]:
    """The editor's values, by field key."""
    out: dict[str, Any] = {k: getattr(p, k) for k in TEXTS}
    for name in _defaulted(p):
        v = p.default(name)
        out[f"default.{name}"] = v if isinstance(v, (bool, int, float, str)) else _word(v)
    if "mode" in p.parameters:
        out["variants"] = "\n".join(f"{w} = {_word(v)}"
                                    for w, v in p.parameters["mode"].choices.items())
    if p.kind == "images":
        out.update({
            "size.step": p.size_step, "size.max_pixels": round(p.max_pixels / 1e6, 2),
            "catalog.models": ", ".join(p.models), "catalog.loras": ", ".join(p.loras),
            "catalog.max_loras": p.max_loras, "catalog.lora_after": p.lora_after or "",
            "catalog.sampler": p.sampler or "",
            "catalog.text": ", ".join(p.text) if p.text else ""})
        for k in ("steps", "cfg", "sampler", "scheduler"):
            v = p.full_settings.get(k, p.full_settings.get("sampler_name") if k == "sampler"
                                    else None)
            out[f"catalog.full.{k}"] = "" if v is None else _word(v)
    return out


def edited(p: Preset, new: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The preset's TOML and workflow with the editor's values in them; PresetError for
    a value that is not one of its field."""
    data, wf = copy.deepcopy(p.source), copy.deepcopy(p.workflow)
    keys = {f["key"] for f in fields(p)}
    bad: list[str] = []
    cat = data.setdefault("catalog", {}) if p.kind == "images" else {}
    full = cat.setdefault("full_settings", {}) if p.kind == "images" else {}
    for key, v in new.items():
        if key not in keys:
            bad.append(f"{key} is not a setting of this preset.")
            continue
        if key in TEXTS:
            data[key] = str(v or "").strip()
        elif key.startswith("default."):
            name = key.removeprefix("default.")
            param = p.parameters[name]
            typ = PARAMETERS[p.kind][name][0]
            if typ == "choice" and param.choices:
                if v not in param.choices:
                    bad.append(f"{v} is not a variant of {p.label}.")
                    continue
                v = param.choices[v]
            elif typ == "choice":
                try:
                    v = json.loads(str(v))
                except ValueError:
                    v = str(v)
            elif typ == "integer" and not (isinstance(v, int) and not isinstance(v, bool)):
                bad.append(f"{PARAMETERS[p.kind][name][1]} is a whole number.")
                continue
            elif typ == "number" and (isinstance(v, bool) or not isinstance(v, (int, float))):
                bad.append(f"{PARAMETERS[p.kind][name][1]} is a number.")
                continue
            elif typ == "boolean":
                v = bool(v)
            elif typ == "text":
                v = str(v or "")
            for path in param.inputs:
                _set(wf, path, v)
        elif key == "variants":
            choices: dict[str, Any] = {}
            for line in str(v or "").splitlines():
                if not line.strip():
                    continue
                word, eq, raw = line.partition("=")
                if not eq or not word.strip() or not raw.strip():
                    bad.append(f"{line.strip()}: a variant is word = value.")
                    continue
                try:
                    choices[word.strip()] = json.loads(raw.strip())
                except ValueError:
                    choices[word.strip()] = raw.strip()
            data["parameters"]["mode"]["choices"] = choices
        elif key.startswith("size."):
            size = data.setdefault("size", {})
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                bad.append("A size is a number above 0.")
            elif key == "size.step":
                size["step"] = int(v)
            else:
                size["max_pixels"] = int(round(float(v) * 1e6))
        elif key in ("catalog.models", "catalog.loras"):
            cat[key.removeprefix("catalog.")] = [x.strip() for x in str(v or "").split(",")
                                                 if x.strip()]
        elif key == "catalog.max_loras":
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                cat["max_loras"] = v
            else:
                bad.append("LoRAs per image is a whole number.")
        elif key in ("catalog.lora_after", "catalog.sampler"):
            cat[key.removeprefix("catalog.")] = str(v or "").strip() or None
        elif key == "catalog.text":
            parts = [x.strip() for x in str(v or "").split(",")]
            cat["text"] = parts if any(parts) else None
        elif key.startswith("catalog.full."):
            k = key.removeprefix("catalog.full.")
            if k == "sampler":
                full.pop("sampler_name", None)
            raw = str(v or "").strip()
            if not raw:
                full.pop(k, None)
                continue
            try:
                full[k] = json.loads(raw) if k in ("steps", "cfg") else raw
            except ValueError:
                bad.append(f"Full model {k} is a number.")
    if bad:
        raise PresetError(bad)
    cat = {k: x for k, x in cat.items() if x not in (None, [], {})}
    if cat:
        data["catalog"] = cat
    else:
        data.pop("catalog", None)
    return data, wf


# ---- the user's presets: var/settings/<folder>/ -------------------------------------------

def save(kind: str, pid: str, data: dict[str, Any], workflow: dict[str, Any],
         origin: str) -> Preset:
    """The preset checked, then written as the user's (TOML and workflow); nothing is
    written when it has a problem (PresetError with every sentence)."""
    from ..settings.files import write_bytes, write_toml
    data = {**data, "workflow": f"{pid}.json"}
    bad: list[str] = []
    p = build(kind, pid, data, workflow, bad, origin)
    if p is None or bad:
        raise PresetError(bad or ["The preset is not valid."])
    root = user_folder(kind)
    write_bytes(root / f"{pid}.json", (json.dumps(workflow, indent=2, ensure_ascii=False)
                                       + "\n").encode())
    write_toml(root / f"{pid}.toml", data)
    return p


def forget(kind: str, pid: str) -> None:
    """The user's files of a preset: an edit (its shipped one is back) or their own."""
    root = user_folder(kind)
    for ext in (".toml", ".json"):
        (root / f"{pid}{ext}").unlink(missing_ok=True)


def upload(kind: str, label: str, text: str) -> Preset:
    """A new preset of the user's from an uploaded workflow with placeholders."""
    label = label.strip()
    if not label:
        raise PresetError(["Give the preset a name."])
    graph, found, task = placeholders(kind, parse_workflow(text))
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "preset"
    taken = {pid for k in FOLDERS for pid in of(k)}
    pid, n = base, 2
    while pid in taken:
        pid, n = f"{base}-{n}", n + 1
    data: dict[str, Any] = {"label": label}
    if kind == "images":
        data["task"] = task
    data["parameters"] = {n: {"inputs": paths} for n, paths in found.items()}
    return save(kind, pid, data, graph, "own")


def replace_workflow(p: Preset, text: str) -> Preset:
    """The preset with another workflow: its placeholders are the parameters now, the
    variants of a mode it still has are kept."""
    graph, found, task = placeholders(p.kind, parse_workflow(text))
    data = copy.deepcopy(p.source)
    if p.kind == "images":
        data["task"] = task
    params: dict[str, Any] = {}
    for name, paths in found.items():
        params[name] = {"inputs": paths}
        if name in p.parameters and p.parameters[name].choices:
            params[name]["choices"] = dict(p.parameters[name].choices)
    data["parameters"] = params
    return save(p.kind, p.id, data, graph, "own" if p.origin == "own" else "edited")


# ---- models and LoRAs (image_catalog.py) ---------------------------------------------

_SAMPLER_KEYS = {"steps": "steps", "cfg": "cfg", "sampler": "sampler_name",
                 "sampler_name": "sampler_name", "scheduler": "scheduler"}


@dataclass(frozen=True)
class Choice:
    """What the catalog resolved for one image."""
    model: Any = None                               # image_catalog.Resource | None
    loras: tuple[tuple[Any, float], ...] = ()       # (Resource, strength)

    @property
    def triggers(self) -> list[str]:
        """The first trigger word of each LoRA: its others are often alternatives
        (low-res or sharp, big or huge) that fight each other when all are sent."""
        return list(dict.fromkeys(r.triggers[0] for r, _ in self.loras if r.triggers))

    def recipe(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.model is not None:
            out["model"] = self.model.id
        if self.loras:
            out["loras"] = [f"{r.id}:{s:g}" for r, s in self.loras]
        if self.triggers:
            out["triggers"] = self.triggers
        return out


def parse_loras(loras: list[str] | None) -> list[tuple[str, float | None]]:
    """Each "name" or "name:strength" as (name, strength or None)."""
    out = []
    for item in (str(x).strip() for x in loras or []):
        ref, _, st = item.rpartition(":")
        if ref and re.fullmatch(r"-?\d+(\.\d+)?", st.strip()):
            out.append((ref, float(st)))
        elif item:
            out.append((item, None))
    return out


def owns(p: Preset, ref: str) -> bool:
    """A model or LoRA name that is in this preset's part of the catalog."""
    return any(ref.strip().startswith(x) for x in p.models + p.loras)


def choose(p: Preset, cat, *, model: str | None, loras: list[str] | None) -> Choice:
    """The model and LoRAs for this image, as given, checked against the preset and the
    catalog. Raises ImageError with the sentence why not."""
    what = {"model": "model", "lora": "LoRA"}
    scope = {"model": p.models, "lora": p.loras}

    def get(kind: str, ref: str):
        r = cat.find(kind, ref, scope[kind])
        if r is None or (r.hidden and kind == "lora"):
            near = cat.nearest(kind, ref, scope[kind])
            hint = f" Close: {', '.join(near)}." if near else ""
            raise ImageError(f"{p.label} has no {what[kind]} {ref!r}.{hint} "
                             f"list_presets lists them.")
        if not r.ready:
            raise ImageError(f"{r.label} is still downloading.")
        return r

    wanted = parse_loras(loras)
    if model and not p.models:
        raise ImageError(f"{p.label} takes no model choice.")
    if wanted and not p.loras:
        raise ImageError(f"{p.label} takes no LoRAs.")
    if len(wanted) > p.max_loras:
        raise ImageError(f"{p.label} takes at most {p.max_loras} LoRAs.")
    m = get("model", model) if model and model.strip() else None
    picked: list[tuple[Any, float]] = []
    for ref, st in wanted:
        r = get("lora", ref)
        if any(r.name == x.name for x, _ in picked):
            continue
        picked.append((r, r.strength if st is None else st))
    if len([r for r, _ in picked if r.category == "character"]) > 1:
        raise ImageError("Use one character LoRA per image.")
    return Choice(model=m, loras=tuple(picked))


def attach(graph: dict[str, Any], p: Preset, choice: Choice) -> dict[str, Any]:
    """The built workflow with the choice in it: the model file and its settings, a
    LoRA chain after `lora_after`, trigger words in front of the prompt."""
    if choice.model is not None:
        for path in p.parameters["model"].inputs:
            _set(graph, path, choice.model.name)
        base = p.full_settings if choice.model.turbo is False else {}
        settings = {**base, **(choice.model.settings or {})}
        if p.sampler:
            for k, v in settings.items():
                if k in _SAMPLER_KEYS:
                    graph[p.sampler]["inputs"][_SAMPLER_KEYS[k]] = v
    if choice.loras and p.lora_after:
        src, prev, ids = [p.lora_after, 0], [p.lora_after, 0], []
        for i, (r, st) in enumerate(choice.loras, 1):
            nid = f"celine_lora_{i}"
            graph[nid] = {"class_type": "LoraLoaderModelOnly",
                          "inputs": {"model": prev, "lora_name": r.name,
                                     "strength_model": st}}
            prev, ids = [nid, 0], ids + [nid]
        for nid, node in graph.items():
            if nid in ids:
                continue
            for k, v in node["inputs"].items():
                if isinstance(v, list) and v == src:
                    node["inputs"][k] = prev
    if choice.triggers and p.text:
        node, key = p.text
        given = graph[node]["inputs"][key]
        low = given.lower() if isinstance(given, str) else ""
        new = [t for t in choice.triggers if t.lower() not in low]
        if new:
            graph["celine_triggers"] = {"class_type": "StringConcatenate", "inputs": {
                "string_a": ", ".join(new), "string_b": given, "delimiter": ". "}}
            graph[node]["inputs"][key] = ["celine_triggers", 0]
    return graph


def listing(s, kind: str) -> list[dict[str, Any]]:
    """The presets of one kind with their place in its role's chain (`order`, `ref`)."""
    chain = [s.split(r) for r in s.chain(ROLES[kind])] if s else []
    out = []
    for p in of(kind).values():
        at = next((n for n, (_, name) in enumerate(chain) if name == p.id), None)
        out.append({**view(p), "order": at,
                    "ref": "/".join(chain[at]) if at is not None else None})
    return out
