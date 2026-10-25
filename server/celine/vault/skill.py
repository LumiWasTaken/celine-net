"""The agent's vault tools (guide skills/guides/vault.md): get, set and rotate an entry. It
never sees a password: a form gets the placeholder vault:<name>/password, which its
browser fills in (skills/browser.py), and a script reads ~/.vault/<name>/password, a
file in its home written through the shellbox's PUT /file (box_files.py).

The file for a script and a new password are high-risk: they wait for the user's tap
(celine/approvals.py) and need its reason (`why`). A new entry, the list and the
placeholder do not ask.

get and rotate take any name an entry has, as the user named it by hand too (vault.pick);
set makes simple lowercase names (NAME).
"""
from __future__ import annotations

import json
import re
from typing import Annotated

import httpx

from .. import box_files
from ..settings.vault import account
from ..skills import ToolContext, ToolResult, guide, tool, described
from .. import vault
from .. import approvals
from . import VaultError, missing, reason

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
NAME_RULE = ("A new vault name is lowercase letters, digits, ., - or _, like example-shop: the "
             "site or the account it is for.")


def step(name: str, which: str = "password") -> str:
    """The browser step that types the entry, ready to copy."""
    return json.dumps({"do": "type", "target": "<field id>", "text": f"vault:{name}/{which}"},
                      ensure_ascii=False)


def _twins(it: dict, others: list[str]) -> list[str]:
    """The line that says which entry it got when others differ from it only by case."""
    if not others:
        return []
    verb = "differs" if len(others) == 1 else "differ"
    return [f"This is {it['name']}; {' and '.join(others)} {verb} from it only by case."]


def _folder(name: str) -> bool:
    """The name works as a folder of ~/.vault: a / in it makes a folder in a folder; an
    empty part, . or .. would put the file somewhere else."""
    return all(part not in ("", ".", "..") for part in name.split("/"))


def _url(it: dict) -> str:
    uris = (it.get("login") or {}).get("uris") or []
    return (uris[0].get("uri") or "") if uris else ""


class VaultSkill:
    name = "vault"
    guide = guide("vault")
    description = described()
    error = "{{username}} has not set up the vault yet (Settings, Vault)."

    app = None      # the running app (core/services.py), from skills.build

    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def status(self) -> str:
        return "up" if account(self.cfg) else "down"

    def _refused(self, e: VaultError) -> ToolResult:
        return ToolResult(content=f"Nothing was done: {reason(e)}. Tell {self.cfg.user}.",
                          is_error=True, display={"refused": True})

    @staticmethod
    def _bad(text: str) -> ToolResult:
        return ToolResult(content=text, is_error=True, display={"refused": True})

    async def _ask(self, ctx: ToolContext, action: str, facts: list[str],
                   why: str, short: str = "") -> ToolResult | None:
        """The user's tap (celine/approvals.py): None when approved, else the result."""
        no = await self.app.approvals.ask(action, facts, ctx.conversation_id, why, short,
                                          call=ctx.tool_call_id or None)
        return None if no is None else self._bad(no)

    def _who(self, it: dict) -> str:
        user = (it.get("login") or {}).get("username") or "no user name"
        url = _url(it)
        return f"{it['name']}: {user}" + (f", {url}" if url else "")

    @tool(read_only=False, destructive=False, open_world=True)
    async def get(
        self,
        ctx: ToolContext,
        name: Annotated[str, "The entry. Leave it out for the list of every entry."] = "",
        shell: Annotated[bool, "Also write the password to ~/.vault/<name>/password in "
                               "your shellbox, for a script. High risk: waits for the "
                               "user's tap and needs why."] = False,
        why: Annotated[str, "With shell: why you need the password in your shellbox, one "
                            "real sentence the user can judge (which script, what for)."] = "",
    ) -> ToolResult:
        """An entry of your vault: its user name and the browser step that types its
        password into a form. Without a name, every entry: name, user name, address.
        Example: vault__get(name="example-shop")."""
        try:
            if not name:
                items = await self.app.vault.items()
                lines = [self._who(it) for it in sorted(items, key=lambda i: i["name"])]
                return ToolResult(content="\n".join(lines) if lines else
                                  "The vault is empty. vault__set(name) makes an entry.",
                                  display={"entries": len(lines)})
            it, others = await self.app.vault.entry(name)
        except VaultError as e:
            return self._refused(e)
        if it is None:
            return self._bad(missing(name))
        name = it["name"]
        lines = [self._who(it), *_twins(it, others), f"Into a password field: {step(name)}"]
        if it.get("passwordHistory"):
            lines.append(f"The one before the last rotate: {step(name, 'previous')}")
        if shell:
            if not (it.get("login") or {}).get("password"):
                return self._bad(f"{name} has no password the vault lets you read: in a "
                                 f"shared collection that hides passwords, members get "
                                 f"none. {self.cfg.user} sets the collection to Edit items "
                                 f"(not hiding passwords) in Vaultwarden. Nothing was asked.")
            if not _folder(name):
                return self._bad(f"{name} cannot be a folder in ~/.vault; the file was not "
                                 f"written. A form takes it as {step(name)}")
            if no := await self._ask(ctx, approvals.SHELL.format(entry=name),
                                     [f"The file: ~/.vault/{name}/password, readable by "
                                      f"everything in the shellbox, the internet included"],
                                     why, approvals.SHELL_SHORT.format(entry=name)):
                return no
            try:
                await box_files.put(self.cfg, ((it.get("login") or {}).get("password") or "").encode(),
                                    f".vault/{name}/password")
            except httpx.HTTPError:
                return self._bad("The shellbox is not reachable; the file was not written.")
            except RuntimeError as e:
                return self._bad(f"The shellbox did not write the file: {e}")
            lines.append(f"For a script: ~/.vault/{name}/password")
        return ToolResult(content="\n".join(lines), display={"entry": name})

    @tool(read_only=False, destructive=False, open_world=True)
    async def set(
        self,
        ctx: ToolContext,
        name: Annotated[str, "The new entry's name: the site or the account, like "
                             "example-shop."],
        username: Annotated[str, "The user name or the email address of the account."] = "",
        url: Annotated[str, "The site's address."] = "",
        notes: Annotated[str, "Anything else to keep with it. Never a password."] = "",
    ) -> ToolResult:
        """Make a vault entry with a new 24-character password; you never see it.
        Returns: the browser step that types it into a form.
        Example: vault__set(name="example-shop", username="celine@example.com",
        url="https://example-shop.com")."""
        if not NAME.match(name):
            return self._bad(NAME_RULE)
        try:
            it, _ = await self.app.vault.entry(name)
            if it is not None:
                return self._bad(f"{it['name']} exists: vault__rotate(name) changes it.")
            it = await self.app.vault.make(name, username.strip(), url.strip(), notes.strip())
        except VaultError as e:
            return self._refused(e)
        return ToolResult(content="\n".join([f"Made {self._who(it)}",
                                             f"Into a password field: {step(name)}"]),
                          display={"entry": name})

    @tool(read_only=False, destructive=True, open_world=True)
    async def rotate(
        self,
        ctx: ToolContext,
        name: Annotated[str, "The entry whose password changes."],
        why: Annotated[str, "Why it changes, one real sentence the user can judge (it "
                            "leaked, the site asks for it). High risk: waits for the "
                            "user's tap."] = "",
    ) -> ToolResult:
        """Give a vault entry a new password; the old one stays in the entry as its
        previous one. Returns: the steps for a change-password form, the old one and
        the new one."""
        try:
            it, others = await self.app.vault.entry(name)
            if it is None:
                return self._bad(missing(name))
            if no := await self._ask(ctx, approvals.ROTATE.format(entry=it["name"]), [
                    f"Its sites: {', '.join(vault.origins(it)) or 'none'}",
                    "The old password stays in the entry as its previous one"], why,
                    approvals.ROTATE_SHORT.format(entry=it["name"])):
                return no
            await self.app.vault.rotate(it)
        except VaultError as e:
            return self._refused(e)
        name = it["name"]
        return ToolResult(content="\n".join([
            f"{name} has a new password.", *_twins(it, others),
            f"The old one: {step(name, 'previous')}",
            f"The new one: {step(name)}",
            f"A script reads the new one once "
            f"vault__get(name={json.dumps(name, ensure_ascii=False)}, shell=true) wrote it."]),
            display={"entry": name})
