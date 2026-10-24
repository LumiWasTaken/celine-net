"""A small TOML writer for the settings files (no dependency; tomllib only reads).

Covers what the settings hold: tables, strings, ints, floats, bools, lists of scalars
or tables, and nested tables. Keys that are not bare are quoted. Output is stable
(insertion order), so a file only changes where a value changed.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def key(k: str) -> str:
    return k if _BARE.match(k) else json.dumps(k, ensure_ascii=False)


def scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise ValueError("NaN and infinity are not allowed in settings")
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)     # TOML basic strings = JSON escapes
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(scalar(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{key(k)} = {scalar(x)}" for k, x in v.items()) + " }" \
            if v else "{}"
    if v is None:
        raise ValueError("None has no TOML form; leave the key out")
    raise ValueError(f"cannot write {type(v).__name__} to TOML")


def _is_table(v: Any) -> bool:
    return isinstance(v, dict)


def _is_table_array(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def dumps(data: dict[str, Any], header: str = "") -> str:
    out: list[str] = []
    if header:
        out.extend(f"# {line}".rstrip() for line in header.splitlines())
        out.append("")
    _table(data, [], out)
    text = "\n".join(out).strip("\n") + "\n"
    return text


def _table(data: dict[str, Any], path: list[str], out: list[str]) -> None:
    plain = {k: v for k, v in data.items()
             if v is not None and not _is_table(v) and not _is_table_array(v)}
    subs = {k: v for k, v in data.items() if _is_table(v)}
    arrays = {k: v for k, v in data.items() if _is_table_array(v)}
    if path and (plain or not subs and not arrays):
        out.append(f"[{'.'.join(key(p) for p in path)}]")
    for k, v in plain.items():
        out.append(f"{key(k)} = {scalar(v)}")
    if plain or (path and not subs and not arrays):
        out.append("")
    for k, v in subs.items():
        _table(v, path + [k], out)
    for k, items in arrays.items():
        for item in items:
            out.append(f"[[{'.'.join(key(p) for p in path + [k])}]]")
            for ik, iv in item.items():
                if iv is not None:
                    out.append(f"{key(ik)} = {scalar(iv)}")
            out.append("")
