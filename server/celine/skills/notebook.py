"""The notebook: its own space, as files, `var/notebook/` (her.notebook).

A sub-agent's conversation may be limited to one folder (`conversations.notebook_folder`,
set by sub_agents/skill.py). The limit is enforced here, in every tool and in the index,
never left to the prompt."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from fnmatch import fnmatch
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from ..turns import ledger
from ..config import CONFIG_DIR
from ..turns.compact import one_line
from . import ToolContext, ToolResult, guide, knob, tool, described
from . import notebook_index
from .anchors import Edit, EditRefused, apply, is_stamp, lines_of, numbered, plan
from ..clock import zone
from ..errors import ToolRefused

# Settings, Advanced, read at use (cfg.r): the index in every system prompt, capped both
# ways, and both caps are told to it
KNOBS = (
    knob("notebook.index_max_entries", "Notes in the index", None, 1, 10_000, 10,
         "The index of its notebook in every system prompt."),
    knob("notebook.index_max_chars", "Index length", "characters", 100, 1_000_000, 4000),
)
# the seeds ship as config/notebook_seed/<rel>, copied once, its own from then on; it reads
# them and never writes or deletes them (owner "config")
SEEDS = {
    "identity.md": "who you are, written by {{username}}",
    "opinions/README.md": "what goes under opinions/",
    "appearance/README.md": "what goes under appearance/",
}
# its own: created empty once, never protected, never re-created once it deletes them;
# appearance/ only with images on, made at the first start that has it on (the folder is
# the mark: its README is a seed, so the folder stays)
PLACEHOLDERS = {
    "appearance/body.md": "(empty. yours to fill, or leave.)\n",
    "appearance/face.md": "(empty. yours to fill, or leave.)\n",
    "appearance/hair.md": "(empty. yours to fill, or leave.)\n",
    "appearance/clothes.md": "(empty. yours to fill, or leave.)\n",
    "opinions/{{username}}.md": "(what you think of {{username}}. yours.)\n",
    "curious.md": None,            # notebook/curious.md, read when seeded
}
NOTES_HEAD = "[your notes]"   # notes_block's first line (turns/legend.py)
UNNAMED = ("", "User")      # the user's name before the first run sets it
WAITING = "placeholders were created, the ones named after the user wait for a name\n"


class Notebook:
    name = "notebook"
    app = None          # the running app (core/services.py), from skills.build
    guide = guide("notebook")
    description = described()

    def __init__(self, cfg):
        self.cfg = cfg
        self.protected = set(SEEDS)
        # the notes the user marked as theirs, toggled from the browser (kv)
        self.marked: set[str] = set()

    def carry(self, old) -> None:
        """A reload's rebuild keeps the marks (reload.py)."""
        self.marked = set(old.marked)

    @property
    def root(self) -> Path:
        """The agent's notebook, the one notebook."""
        return self.cfg.her.notebook

    async def message_context(self, ctx: ToolContext, message: dict):
        """`[your memory]`: the sections of its notes a new user message brings up,
        graded (skills/recall.py)."""
        from . import recall
        return await recall.block(self.cfg, ctx.store, ctx.conversation_id, ctx.turn_id,
                                  message)

    async def load_marks(self, store) -> None:
        raw = await store.kv_get("notebook_protected")
        self.marked = set(json.loads(raw)) if raw else set()

    async def mark(self, store, path: str, protected: bool) -> None:
        rel = str(Path(path))
        if protected:
            self.marked.add(rel)
        else:
            self.marked.discard(rel)
        await store.kv_set("notebook_protected", json.dumps(sorted(self.marked)))

    def tag(self, path: str) -> str:
        """What to tell it after a path: nothing when it is its own."""
        owner = self.owner(path)
        if owner == "generated":
            return "  [generated, read-only]"
        if owner:
            return self.cfg.substitute("  [{{username}}'s, you can read it, not change it]")
        return ""

    def owner(self, path: str) -> str | None:
        """'config', 'marked', 'generated', or None when the file is its own."""
        rel = str(Path(path))
        if rel == "tools" or rel.startswith("tools/"):
            return "generated"
        if rel in self.protected:
            return "config"
        if rel in self.marked:
            return "marked"
        return None

    def virtual(self) -> dict[str, str]:
        """The tools/ folder of the notebook browser (for {{username}}, not it): every
        skill's reference."""
        from .toolsdoc import render
        registry = getattr(self.app, "registry", None)      # the running app's
        return render(registry, self.cfg) if registry is not None else {}

    async def seed(self, store) -> list[str]:
        return await self.seed_one(store)

    async def seed_one(self, store) -> list[str]:
        """Write the files {{username}} owns if they are missing. Never overwrites."""
        from ..settings import features
        made = []
        self.root.mkdir(parents=True, exist_ok=True)
        images = features.on(self.cfg, "images")
        looks = images and not (self.root / "appearance").exists()
        for rel, summary in SEEDS.items():
            target = self.root / rel
            if target.exists() or (rel.startswith("appearance/") and not images):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # kept as written: {{name}} and {{username}} are filled in when read, so
            # the names set on the first run (after this seed) are the ones it reads
            body = (CONFIG_DIR / "notebook_seed" / rel).read_text()
            target.write_text(body)
            await notebook_index.upsert(store, rel, self.cfg.substitute(summary),
                                         len(body), body)
            made.append(rel)
        for rel, body in PLACEHOLDERS.items():
            if rel.startswith("appearance/") and looks:
                made.append(await self._placeholder(store, rel, body))
        marker = self.root / ".placeholders"
        first = not marker.exists()
        if first or marker.read_text() == WAITING:
            waiting = False
            for rel, body in PLACEHOLDERS.items():
                named = "{{username}}" in rel
                if rel.startswith("appearance/") or (not first and not named):
                    continue
                if named and self.cfg.user in UNNAMED:
                    waiting = True          # named after the user, once there is a name
                    continue
                rel = self.cfg.substitute(rel)
                if not (self.root / rel).exists():
                    made.append(await self._placeholder(store, rel, body))
            marker.write_text(WAITING if waiting else "placeholders were created once\n")
        return made

    async def _placeholder(self, store, rel: str, body: str | None) -> str:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        body = self.cfg.substitute(body or (CONFIG_DIR / "notebook_seed" / rel).read_text())
        target.write_text(body)
        await notebook_index.upsert(store, rel, "placeholder, not written yet", len(body), body)
        return rel

    async def reconcile(self, store) -> dict[str, int]:
        """The files and the index agree at startup. A
        text file the index does not know (restored from a backup, written outside the
        app) is indexed with a stand-in summary; a file whose size changed outside the
        app is re-indexed; an entry whose file is gone is dropped. tools/ is generated
        and skipped."""
        return await self._reconcile_one(store)

    async def _reconcile_one(self, store) -> dict[str, int]:
        rows = {r["path"]: r for r in await notebook_index.list(store)}
        added = changed = removed = 0
        if not self.root.is_dir():
            return {"added": 0, "changed": 0, "removed": 0}
        for f in sorted(self.root.rglob("*")):
            if not f.is_file() or any(part.startswith(".") for part in
                                      f.relative_to(self.root).parts):
                continue
            rel = f.relative_to(self.root).as_posix()
            if self.owner(rel) == "generated":
                continue
            try:
                body = f.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            if rel not in rows:
                await notebook_index.upsert(store, rel, "found on disk, no summary yet",
                                             len(body), body)
                added += 1
            elif rows[rel]["bytes"] != len(body):
                await notebook_index.upsert(store, rel, rows[rel]["summary"], len(body),
                                             body)
                changed += 1
        for rel in rows:
            if not (self.root / rel).is_file():
                await notebook_index.delete(store, rel)
                removed += 1
        return {"added": added, "changed": changed, "removed": removed}

    def _owned_by_user(self, path: str) -> bool:
        return self.owner(path) is not None

    def identity(self) -> str | None:
        """identity.md with its name and the user's filled in as they are now."""
        p = self.root / "identity.md"
        return self.cfg.substitute(p.read_text()) if p.exists() else None

    def _resolve(self, path: str, folder: str | None = None) -> Path:
        """Confine to the notebook root, symlinks included, and to `folder` when the
        conversation is limited to one. ToolRefused, the tool's answer, when not."""
        p = (self.root / path).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise ToolRefused(f"{path} is outside your notebook.")
        # the app's own files (.index.db, .versions, .trash: deleted notes are the
        # user's to bring back) are not notes
        if any(part.startswith(".") for part in p.relative_to(self.root.resolve()).parts):
            raise ToolRefused(f"{path} is not in your notebook: notebook__list shows your "
                              f"notes.")
        if folder and not p.is_relative_to((self.root / folder).resolve()):
            raise ToolRefused(f"{path} is outside your folder {folder}.")
        return p

    def _place(self, path: str, folder: str | None) -> tuple[Path, str]:
        """`_resolve`, and the path relative to the notebook as the index keeps it."""
        p = self._resolve(path, folder)
        return p, p.relative_to(self.root.resolve()).as_posix()

    @tool(read_only=False, destructive=True, needs_answer=False)
    async def write(
        self,
        ctx: ToolContext,
        path: Annotated[str, "A relative path, such as people/noor.md. Missing folders are "
                             "created."],
        content: Annotated[str, "The text: the whole note (replace) or the text to add "
                                "(append)."],
        summary: Annotated[str, "One line for your index."],
        mode: Annotated[Literal["replace", "append"], "replace or append."] = "replace",
    ) -> ToolResult:
        """Write one note. `replace` creates the note or replaces all its text. `append`
        adds text to its end. To change some lines, use notebook__edit. Write what
        {{username}} asks you to remember, their preferences and corrections, your plans,
        your diary. Do not copy chat messages: keep references. Answer first, then write,
        and do not end your reply with only a line that you wrote it. You cannot write
        {{username}}'s notes. Head lines right under the first line steer a note:
        `keys: ram, laptop` loads it when one comes up, `load: always`, `class:
        public, informal, nsfw or sensitive`."""
        try:
            p, path = self._place(path, await folder_of(ctx))
        except ToolRefused as e:
            return ToolResult(content=str(e), is_error=True)
        if self.owner(path) == "generated":
            return ToolResult(content="tools/ is not part of your notebook.", is_error=True)
        if self._owned_by_user(path):
            return ToolResult(content=self.cfg.substitute(
                f"{path} is {{{{username}}}}'s, not yours to change. You can read it. "
                f"Write your own version somewhere else if you want one."), is_error=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        body = stamped(p.read_text() + content if mode == "append" and p.exists()
                       else content, self.cfg)
        keep_version(self.root, p, path)
        p.write_text(body)
        await notebook_index.upsert(ctx.store, path, summary, len(body), body)
        await _touch(ctx, path)
        return ToolResult(content=f"Saved {path}.",
                          display={"path": path, "bytes": len(body), "mode": mode})

    @tool()
    async def read(
        self,
        ctx: ToolContext,
        path: Annotated[str, "The path, from your index or notebook__list."],
        from_line: Annotated[int, "Start at this line, for the next part."] = 1,
    ) -> ToolResult:
        """Read one note. Each line comes as `<n>:<hash>| <text>`. `n:hash` is the anchor
        for notebook__edit. Line 1, Last changed, comes plain: the app keeps it. A long
        note comes in parts. You can read {{username}}'s notes too."""
        try:
            p, path = self._place(path, await folder_of(ctx))
        except ToolRefused as e:
            return ToolResult(content=str(e), is_error=True)
        if self.owner(path) == "generated":
            return ToolResult(content="Skill guides are not notes in your notebook. "
                                      "skills__read_guide(name=\"<skill>\") shows a skill's "
                                      "guide.", is_error=True)
        if p.is_dir():
            return _folder(path)
        if not p.exists():
            return ToolResult(content=f"{path} does not exist.{_near_names(p, path)} "
                                      f"notebook__list shows your notes. notebook__search "
                                      f"finds one by its words.", is_error=True)
        text = p.read_text(newline="")       # reading leaves the index's order alone
        head = ""
        if self._owned_by_user(path):
            text = self.cfg.substitute(text)  # a seeded note names it as it is now
            head = self.cfg.substitute(f"({path} is {{{{username}}}}'s. You can read it, "
                                       f"not change it.)\n\n")
        elif _is_placeholder(text):
            head = f"({path} is yours and still a placeholder. Fill it in whenever you want.)\n\n"
        lines = lines_of(text)[0]
        first = max(1, from_line)
        if lines and first > len(lines):
            return ToolResult(content=f"{path} has {len(lines)} lines.", is_error=True)
        body = "\n".join(numbered(lines, first, len(lines)))
        if ctx.max_output_tokens:
            from ..turns import tokens
            cut, shown, total = tokens.cut_to(body, max(500, int(ctx.max_output_tokens) - 200))
            if shown < total:
                last = first + cut.count("\n")
                body = (cut + f"\n\n[lines {first} to {last} of {len(lines)}. Next part: "
                        f"notebook__read(path=\"{path}\", from_line={last + 1})]")
        if not lines:
            body = "(empty)"
        content = head + body
        led = ledger.get(ctx.turn_id, ledger.NOTEBOOK)
        args = {"path": path, "from_line": from_line}
        led.record("read", led.spec("read", args), content, search=False,
                   refs=ledger.note_refs("read", args, content))
        await _touch(ctx, path, read=True)
        return ToolResult(content=content, display={"path": path, "bytes": len(text),
                                                    "owner": self.owner(path),
                                                    "class": class_line(text)})

    @tool(read_only=False, destructive=True, needs_answer=False)
    async def edit(
        self,
        ctx: ToolContext,
        path: Annotated[str, "The path of an existing note."],
        edits: Annotated[list[Edit], "The changes: replace, insert or delete, each with "
                                     "anchors."],
        summary: Annotated[str, "A new index line. Leave it empty to keep the old "
                                "one."] = "",
    ) -> ToolResult:
        """Change lines of one note by their anchors from notebook__read. Read the note
        first. All edits in one call refer to the note as you read it. If an anchor does
        not match, nothing changes and you get the current lines."""
        try:
            p, path = self._place(path, await folder_of(ctx))
        except ToolRefused as e:
            return ToolResult(content=str(e), is_error=True)
        if self.owner(path) == "generated":
            return ToolResult(content="tools/ is not part of your notebook.", is_error=True)
        if self._owned_by_user(path):
            return ToolResult(content=self.cfg.substitute(
                f"{path} is {{{{username}}}}'s, not yours to change. You can read it. "
                f"Write your own version somewhere else if you want one."), is_error=True)
        if p.is_dir():
            return _folder(path)
        if not p.exists():
            return ToolResult(content=f"{path} does not exist. notebook__write makes a new "
                                      f"note.", is_error=True)
        text = p.read_text(newline="")
        lines, newline, trailing = lines_of(text)
        has_stamp = bool(lines) and is_stamp(lines[0])
        edits = [{**e, "text": _joined(e["text"])}
                 if isinstance(e, dict) and e.get("text") else e for e in edits]
        try:
            planned = plan(edits, lines, has_stamp)
        except EditRefused as e:
            return ToolResult(content=f"Nothing changed in {path}. {e}", is_error=True)
        new, regions = apply(lines, planned)
        if has_stamp:
            new[0] = stamp_line(self.cfg)
        else:
            new.insert(0, stamp_line(self.cfg))
            regions = [(a + 1, b + 1) for a, b in regions]
        body = newline.join(new) + (newline if new and (trailing or not lines) else "")
        keep_version(self.root, p, path)
        p.write_text(body, newline="")
        if not summary:
            summary = await notebook_index.summary(ctx.store, path) or ""
        await notebook_index.upsert(ctx.store, path, summary, len(body), body)
        await _touch(ctx, path)
        n = len(planned)
        shown = "\n...\n".join("\n".join(numbered(new, a, b)) or "(the note is empty now)"
                                for a, b in regions)
        return ToolResult(content=f"Saved {path}. {n} {'change' if n == 1 else 'changes'}, "
                                  f"{len(new)} lines now. The changed part, with its new "
                                  f"anchors:\n\n{shown}",
                          display={"path": path, "bytes": len(body), "changes": n})

    @tool()
    async def search(
        self,
        ctx: ToolContext,
        query: Annotated[str, "The words to search for."],
        limit: Annotated[int, "The most notes."] = 10,
    ) -> ToolResult:
        """Search your notes by word search over paths, summaries and text. Search before
        you write a fact. Search when {{username}} asks what you know. Returns: one line
        per note with a snippet."""
        # searches that keep finding no note it has not seen get a note, then are not
        # run, as in the Messages archive (turns/ledger.py); a read with new text resets it
        led = ledger.get(ctx.turn_id, ledger.NOTEBOOK)
        spec = led.spec("search", {"query": query, "limit": limit})
        if stop := led.blocked(broad=False):
            led.refuse(spec, broad=False)
            return ToolResult(content=stop, is_error=True, display={"narrow": True})
        call = led.begin("search", spec, search=True)
        folder = await folder_of(ctx)
        if folder:
            hits = [h for h in await notebook_index.search(ctx.store, query,
                                                           limit * 4)
                    if _inside(h["path"], folder)][:limit]
        else:
            hits = await notebook_index.search(ctx.store, query, limit)
        display = {"hits": len(hits)}
        if hits:                                 # who may see these notes (Bastet)
            from ..guard.facts import strictest
            files = [self.root / h["path"] for h in hits]
            display["class"] = strictest(class_line(f.read_text()) if f.is_file()
                                         else UNCLASSED for f in files)
        if hits:
            content = "\n".join(
                f"{h['path']} - {one_line(h['summary'])}{self.tag(h['path'])}\n"
                f"  ...{one_line(h['snippet'])}..." for h in hits)
        else:
            content = f"{ledger.NO_NOTE} {query!r}."
        led.finish(call, content, ledger.note_refs("search", {}, content))
        n, _ = led.dry_streak()
        if n >= ledger.NUDGE_AFTER:
            content += (f"\n\n{n} searches of your notes in a row found no note you have "
                        f"not seen. Other words will not change that. Read the notes you "
                        f"found with notebook__read, or answer with what you have and say "
                        f"what is missing. After {ledger.STOP_AFTER} in a row, no search "
                        f"is run.")
            display["narrow"] = True
        return ToolResult(content=content, display=display)

    @tool()
    async def list(
        self,
        ctx: ToolContext,
        folder: Annotated[str | None, "Only notes under this folder, such as "
                                      "people."] = None,
    ) -> ToolResult:
        """List your notes, each with the day it last changed, its summary and its head
        lines ([keys: ...], [class], or [no keys]), all or one folder. A note without a
        tag is yours."""
        limit = await folder_of(ctx)
        if limit:
            if folder:
                try:
                    self._resolve(folder, limit)
                except ToolRefused as e:
                    return ToolResult(content=str(e), is_error=True)
            else:
                folder = limit
        rows = await notebook_index.list(ctx.store, folder)
        tz = zone(self.cfg)
        lines = [f"{r['path']} ({_day(r['touched_at'], tz)}) - {r['summary']}"
                 f"{self.tag(r['path'])}{self._heads(r['path'])}" for r in rows]
        if not lines:
            if limit or folder:
                return ToolResult(content=f"{folder} is empty. Write whatever you want in it.")
            return ToolResult(content="Your notebook is empty. Write whatever you want in it.")
        return ToolResult(content="\n".join(lines), display={"count": len(lines)})

    @tool(read_only=False, destructive=True)
    async def delete(
        self,
        ctx: ToolContext,
        path: Annotated[str, "The path of the note."],
    ) -> ToolResult:
        """Delete one of your notes. {{username}}'s notes cannot be deleted."""
        try:
            p, path = self._place(path, await folder_of(ctx))
        except ToolRefused as e:
            return ToolResult(content=str(e), is_error=True)
        if self.owner(path) == "generated":
            return ToolResult(content="tools/ is not part of your notebook.", is_error=True)
        if self._owned_by_user(path):
            return ToolResult(content=self.cfg.substitute(
                f"{path} is {{{{username}}}}'s. It stays."), is_error=True)
        if p.is_dir():
            return _folder(path)
        if not p.exists():
            return ToolResult(content=f"{path} does not exist. notebook__list shows your "
                                      f"notes.", is_error=True)
        trash(self.root, p, path)
        await notebook_index.delete(ctx.store, path)
        return ToolResult(content=f"Deleted {path}.", display={"path": path})

    @tool(read_only=False, destructive=True)
    async def move(
        self,
        ctx: ToolContext,
        src: Annotated[str, "The note or folder to move."],
        dst: Annotated[str, "The full new path. Missing folders are created."],
    ) -> ToolResult:
        """Change the path of a note or a folder. Your index follows. {{username}}'s notes
        do not move."""
        folder = await folder_of(ctx)
        try:
            (a, src), (b, dst) = self._place(src, folder), self._place(dst, folder)
        except ToolRefused as e:
            return ToolResult(content=str(e), is_error=True)
        for rel in (src, dst):
            if rel == "tools" or rel.startswith("tools/"):
                return ToolResult(content="tools/ is not part of your notebook.",
                                  is_error=True)
        users = [p for p in sorted(self.protected | self.marked) if _inside(p, src)]
        if users:
            return ToolResult(content=self.cfg.substitute(
                f"{users[0]} is {{{{username}}}}'s. It stays where it is."), is_error=True)
        if a == self.root.resolve() or (folder and a == (self.root / folder).resolve()):
            return ToolResult(content="That is the whole folder you work in. Move what is "
                                      "inside it instead.", is_error=True)
        if not a.exists():
            return ToolResult(content=f"{src} does not exist. notebook__list shows your "
                                      f"notes.", is_error=True)
        if b.exists():
            return ToolResult(content=f"{dst} already exists. Pick another path, or move "
                                      f"that one away first.", is_error=True)
        if a.is_dir() and b.is_relative_to(a):
            return ToolResult(content="A folder cannot move into itself.", is_error=True)
        move_note(self.root, a, b, src, dst)
        n = await notebook_index.move(ctx.store, src, dst)
        return ToolResult(content=f"Moved {src} to {dst} ({n} index "
                                  f"{'entry' if n == 1 else 'entries'} followed)",
                          display={"src": src, "dst": dst, "entries": n})

    def _heads(self, rel: str) -> str:
        """` [keys: a, b] [class]` of one note, ` [no keys]` without keys."""
        keys = keys_of(self.root, rel)
        cls = class_line(read_note(self.root, rel, head=True) or "")
        out = f" [keys: {', '.join(keys)}]" if keys else " [no keys]"
        return out + (f" [{cls}]" if cls != UNCLASSED else "")

    async def prompt_fragment(self, ctx: ToolContext) -> str | None:
        parts = []
        if identity := self.identity():
            parts.append(self.cfg.substitute(
                "identity.md, from your notebook ({{username}} wrote it, you read it):\n\n")
                + identity.strip())
        folder = await folder_of(ctx)
        if index := await build_index_fragment(ctx.store, self.cfg, self.tag,
                                               folder, ctx.loaded_notes):
            parts.append(index)
        return "\n\n".join(parts) or None


async def _touch(ctx: ToolContext, path: str, read: bool = False) -> None:
    """A note it wrote or edited is loaded in full from its next turn on, and stays
    while it uses it (turns/loads.py). A note it only
    `read` stays for its next turn alone: touched as if `idle_turns` less one turns ago,
    unless a write keeps it longer."""
    if ctx.store is None:
        return
    from ..turns import loads
    if not read:
        await loads.touch(ctx.store, ctx.conversation_id, "note", [path])
        return
    idle = int(loads.shares(ctx.config)["idle_turns"])
    cid = ctx.conversation_id

    def fn(c) -> None:
        before = loads.rows(c, cid)[1].get(("note", path))
        loads.touch_sync(c, ctx.store, cid, "note", [path])
        got = loads.rows(c, cid)[1].get(("note", path))
        if got is not None:
            aged = got - idle + 1
            c.execute("UPDATE context_loaded SET touched=? WHERE conversation_id=? AND "
                      "kind='note' AND name=?",
                      (max(aged, before if before is not None else aged), cid, path))
    await ctx.store.write(fn)


def _day(at: str | None, tz) -> str:
    """"16 Oct": the day a stored time falls on in the app zone."""
    if not at:
        return "?"
    return f"{datetime.fromisoformat(at).astimezone(tz):%-d %b}"


async def folder_of(ctx: ToolContext) -> str | None:
    """The notebook folder this conversation is limited to (a sub-agent's), or None
    for the whole notebook."""
    def fn(c):
        r = c.execute("SELECT notebook_folder FROM conversations WHERE id=?",
                      (ctx.conversation_id,)).fetchone()
        return r["notebook_folder"] if r else None
    return await ctx.store.read(fn)


def _joined(text) -> str:
    """An edit's `text` as one string: a list of lines, as it sometimes sends it, is
    joined."""
    return "\n".join(map(str, text)) if isinstance(text, list) else text


def _near_names(p: Path, path: str) -> str:
    """For a note that does not exist: up to 5 notes in its folder whose names share its
    first word or its month, as " radar/ has: radar/a.md, radar/b.md."."""
    folder = p.parent
    if not folder.is_dir():
        return ""
    date = re.search(r"\d{4}-\d{2}", p.stem)
    start = re.split(r"[-_. ]", p.stem)[0]
    names = sorted((f.name for f in folder.iterdir() if f.is_file()
                    and not f.name.startswith(".")
                    and ((start and f.name.startswith(start))
                         or (date and date[0] in f.name))), reverse=True)[:5]
    if not names:
        return ""
    where = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
    return f" {where or 'Your notebook'} has: {', '.join(where + n for n in names)}."


def _folder(path: str) -> ToolResult:
    return ToolResult(content=f"{path} is a folder. notebook__list(folder=\"{path}\") "
                              f"shows its notes.", is_error=True)


def _inside(path: str, folder: str) -> bool:
    folder = folder.rstrip("/")
    return path == folder or path.startswith(folder + "/")


def _is_placeholder(text: str) -> bool:
    text = unstamped(text)
    return text.strip().startswith("(") and "yours" in text[:120]


# Line 1 of every note: when it last changed, in the app's time zone. The code writes it
# on every change (its tools and the notebook browser); its edits cannot touch it, and a
# stamp inside what it writes is replaced. A note without one gets it on its next change.
def _now() -> datetime:
    return datetime.now(timezone.utc)


def keep_version(root: Path, p: Path, rel: str) -> None:
    """Copy the current text to .versions/<path>/<time>.md before it changes."""
    if p.is_file():
        dest = root / ".versions" / rel / f"{_now():%Y-%m-%d_%H%M%S_%f}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(p.read_bytes())


def move_note(root: Path, a: Path, b: Path, src: str, dst: str) -> None:
    """A note or folder to its new path; its .versions history follows."""
    b.parent.mkdir(parents=True, exist_ok=True)
    a.rename(b)
    old = root / ".versions" / src
    if old.exists():
        new = root / ".versions" / dst
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)


def trash(root: Path, p: Path, rel: str) -> None:
    """A note or folder into .trash/<time>/<path>, never deleted."""
    dest = root / ".trash" / f"{_now():%Y-%m-%d_%H%M%S_%f}" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    p.rename(dest)


def stamp_line(cfg) -> str:
    return f"Last changed: {_now().astimezone(zone(cfg)):%Y-%m-%d %H:%M}"


def unstamped(text: str) -> str:
    """`text` without its Last changed line."""
    first, _, rest = text.partition("\n")
    return rest if is_stamp(first) else text


def stamped(text: str, cfg) -> str:
    """`text` with a fresh stamp as its first line, in place of one it already has."""
    newline = "\r\n" if "\r\n" in text else "\n"
    return stamp_line(cfg) + newline + unstamped(text)


CLASSES = ("public", "informal", "nsfw", "sensitive")   # who may see a note (gates)
UNCLASSED = "unclassed"     # a note without a class line: the leak rule judges its text


def head_line(text: str, key: str) -> list[str] | None:
    """What a note's `key:` head line says, lowercased and split at commas; None without
    one. Head lines are those before its first blank line, after the Last changed line
    (a `---` line around them is fine): `load:`, `keys:`, `class:`."""
    pat = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.*?)\s*$", re.I)
    for line in unstamped(text).lstrip("\r\n").splitlines():
        if not line.strip():
            break
        if m := pat.match(line):
            return [w.strip().lower() for w in m[1].split(",") if w.strip()]
    return None


def load_line(text: str) -> list[str] | None:
    """What a note's `load:` line says: ["always"] or ["off"]; None without one (loaded
    by need, turns/loads.py)."""
    return head_line(text, "load")


def keys_line(text: str) -> list[str]:
    """The words of a note's `keys:` line: it loads when one comes up in the talk."""
    return head_line(text, "keys") or []


def class_line(text: str) -> str:
    """A note's class (CLASSES) from its `class:` line; UNCLASSED without a valid one."""
    v = head_line(text, "class") or []
    return v[0] if len(v) == 1 and v[0] in CLASSES else UNCLASSED


def inside(path: str, folder: str | None) -> bool:
    """Whether note `path` is in `folder` (a sub-agent's); every note is without one."""
    if not folder:
        return True
    folder = folder.rstrip("/")
    return path == folder or path.startswith(folder + "/")


def read_note(root: Path, rel: str, head: bool = False) -> str | None:
    """A note's text (its first 2,000 characters with `head`), None when it is gone or
    outside the notebook."""
    p = (root / rel).resolve()
    if not p.is_file() or not p.is_relative_to(root.resolve()):
        return None
    with p.open(encoding="utf-8", errors="replace") as f:
        return (f.read(2000) if head else f.read()).strip()


def carried_text(text: str) -> str:
    """What it carries of a note: everything before a `## earlier` heading (the older
    voice samples, conversations/samples.py, stay in the file, not in its context)."""
    at = text.find("\n## earlier\n")
    return text if at < 0 else text[:at].rstrip()


def load_of(rel: str, head: str, user: str) -> str | None:
    """What a note's head says: "always", "off", or None (by need). The notes on the
    user, `people/<user>.md` and `people/<user>-*.md`, are always loaded (not
    `people/<user>s-<name>.md`)."""
    load = load_line(head)
    if load is not None:
        return "always" if "always" in load else "off"
    mine = user and (rel == f"people/{user}.md" or fnmatch(rel, f"people/{user}-*.md"))
    return "always" if mine else None


def keys_of(root: Path, rel: str) -> list[str]:
    """A note's `keys:` words (keys_line), read again only when the note's mtime
    changed."""
    p = root / rel
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        return []
    return list(_keys_at(root, rel, mtime))


@lru_cache(maxsize=4096)
def _keys_at(root: Path, rel: str, mtime: int) -> tuple[str, ...]:
    return tuple(keys_line(read_note(root, rel, head=True) or ""))


def notes_block(cfg, notes: list[dict], head: bool = True) -> str | None:
    """The notes it carries in full (turns/loads.py) as one block, by path so the block
    changes only where a note comes or goes; the ones over the share and the ones the
    user left out here named in one line each. `head`: the line that says what they are
    (the system prompt's block; the by-need ones in the tail go without)."""
    from ..turns.loads import OFF_HERE
    loaded = sorted((n for n in notes if n["state"] == "included"), key=lambda n: n["path"])
    over = [n["path"] for n in notes if n["state"] == "left_out"]
    off = [n["path"] for n in notes if n["why"] == OFF_HERE]
    if not loaded and not over and not off:
        return None
    parts = [cfg.substitute(
        f"{NOTES_HEAD} loaded now: your notes about {{{{username}}}} always, other notes "
        "while the talk needs them.") if head else
        f"{NOTES_HEAD} loaded for now, the talk needs them:"]
    parts += [f"### {n['path']}\n{n['text']}" for n in loaded]
    if over:
        parts.append(f"Not loaded, no room for them now: {', '.join(over)}.")
    if off:
        parts.append(cfg.substitute(f"Left out by {{{{username}}}} in this conversation: "
                                    f"{', '.join(off)}."))
    return "\n\n".join(parts)


async def build_index_fragment(store, cfg, tag=lambda path: "",
                               folder: str | None = None,
                               loaded: frozenset[str] = frozenset()) -> str | None:
    """The agent's index, injected every turn. Capped two ways, and both caps are told to.
    `map.md` at the root (of `folder`, when the conversation is limited to one) comes
    first whenever it exists: its own table of contents. In a limited conversation
    only entries inside the folder are shown. The notes `loaded` in full are left out:
    they are in its request already (the always ones in the system prompt, the others
    in the tail)."""
    max_entries = int(cfg.r("notebook.index_max_entries"))
    max_chars = int(cfg.r("notebook.index_max_chars"))

    rows = [r for r in await notebook_index.list(store, folder, order="touched")
            if r["path"] not in loaded]
    total = len(rows)
    if not total:
        return None
    map_path = f"{folder.rstrip('/')}/map.md" if folder else "map.md"
    rows.sort(key=lambda r: r["path"] != map_path)       # stable: map.md first

    lines: list[str] = []
    used = shown = crowded = 0
    for row in rows:
        if shown >= max_entries:
            break
        line = f"    {row['path']} - {row['summary']}{tag(row['path'])}"
        if used + len(line) > max_chars:
            crowded += 1
            continue
        lines.append(line)
        used += len(line)
        shown += 1

    hidden = max(0, total - shown)
    where = f"Your notebook, limited to {folder}" if folder else "Your notebook"
    head = cfg.substitute(
        f"{where} ({shown} of {total} entries, most recently changed first). Every note "
        f"without a tag is yours, written by you. The tagged notes are "
        f"{{{{username}}}}'s or generated:")
    out = [head, "", *lines, ""]
    if hidden:
        out.append(f"{hidden} more {'entry is' if hidden == 1 else 'entries are'} "
                   f"not shown. notebook__search finds them.")
    if crowded:
        out.append(f"{crowded} did not fit in your index budget "
                   f"({max_chars} characters). Shorter summaries let more of them fit.")
    return "\n".join(out)


NO_SUMMARY = "(no summary yet)"


async def write_note(cfg, store, ch, p: Path, rel: str, text: str) -> str:
    """A note in its notebook (`ch` is its row) written by you: the text it had is kept
    in .versions first (its notebook's history), the new one stamped and indexed."""
    keep_version(ch.notebook.resolve(), p, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    content = stamped(text, cfg)
    p.write_text(content)
    summary = await notebook_index.summary(store, rel) or NO_SUMMARY
    await notebook_index.upsert(store, rel, summary, len(content), content)
    return content
