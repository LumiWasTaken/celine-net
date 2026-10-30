"""A reply goes with its thinking in every request (turns/context.py), so a model's
`keep_thinking` is gone from settings/providers.toml (beside celine.db). A file without
it stays as it is; run again, it writes nothing."""
from __future__ import annotations

import tomllib
from pathlib import Path


def up(conn, db_path) -> None:
    from celine.settings.files import write_toml
    path = Path(db_path).parent / "settings" / "providers.toml"
    if not path.is_file():
        return
    with path.open("rb") as f:
        data = tomllib.load(f)
    changed = False
    for provider in (data.get("providers") or {}).values():
        for model in ((provider or {}).get("models") or {}).values():
            if isinstance(model, dict) and "keep_thinking" in model:
                del model["keep_thinking"]
                changed = True
    if changed:
        write_toml(path, data)
