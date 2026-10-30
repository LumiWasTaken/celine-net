"""A sub-agent whose briefs an agent CLI in its shellbox works on, instead of a model in
a conversation of its own (sub_agents__start with `cli`).

Its turns run like any sub-agent's, on this backend: the brief (every message it got
since its last answer) goes into ~/agents/<its terminal>/brief.md, and in its own
terminal `agent <cli> -m <model> --minutes <cli_minutes> -f brief.md -o answer.md`
(shellbox/agent.py) runs there, the CLI calling its models through the app's model
proxy. The run's answer.md is the reply, so it comes back as the report like any
sub-agent's; when the CLI leaves none, the end of its output. A Stop presses Ctrl-C in
its terminal. The CLI's own minutes are not the turn's: the turn's clock moves with
them. The folder stays: a new brief to the same sub-agent runs in it again.
"""
from __future__ import annotations

import anyio

from .. import metrics
from ..backends.base import BackendError, Done, TextDelta
from ..settings import CONVERSATION_MODELS, her_models

CLIS = ("opencode",)    # the one list; shellbox/agent.py and the guides match
POLL = 5.0              # seconds between looks at a run that outlived one /run
ANSWER_MAX = 256 * 1024     # bytes of answer.md read


def brief_of(c, cid: str) -> str:
    """What it was given since its last answer: the user messages after its last reply
    that said anything (the turn's own reply is already there, empty)."""
    rows = c.execute(
        "SELECT content FROM messages WHERE conversation_id=? AND role='user' AND "
        "rowid > coalesce((SELECT max(rowid) FROM messages WHERE conversation_id=? AND "
        "role='assistant' AND coalesce(content, '') <> ''), 0) ORDER BY rowid",
        (cid, cid)).fetchall()
    return "\n\n".join(str(r["content"] or "") for r in rows).strip()


class CliBackend:
    supports_tools = False
    supports_images = False

    def __init__(self, services, cid: str, cli: str, clock=None):
        self.services, self.cid, self.cli, self.clock = services, cid, cli, clock
        s = services.cfg.settings
        own = CONVERSATION_MODELS.get(cid)
        self.model = own if own in her_models.allowed(s) else "default"
        self.name = f"cli:{cli}"

    @property
    def shell(self):
        reg = self.services.registry
        return reg.skills.get("shell") if reg is not None else None

    async def stream(self, messages, tools, body=None):
        from .. import box_files, shellbox
        from ..terminals import store as terminals
        shell = self.shell
        if shell is None or not shellbox.url(self.services.cfg):
            raise BackendError("The shellbox is not set up, so no agent CLI can run.",
                               retryable=False)
        store = self.services.store
        brief = await store.read(lambda c: brief_of(c, self.cid))
        if not brief:
            raise BackendError("There is no brief to work on.", retryable=False)
        term = await terminals.default_terminal(store, self.cid)
        folder = f"agents/{term}"
        minutes = float(self.services.cfg.r("sub_agents.cli_minutes"))
        if self.clock is not None:          # the CLI's minutes are its own, not the turn's
            self.clock.deadline = max(self.clock.deadline,
                                      anyio.current_time() + minutes * 60 + 300)
        try:
            await box_files.put(self.services.cfg, brief.encode(), f"{folder}/brief.md",
                                transport=shell.transport)
        except Exception as e:              # noqa: BLE001 - the box's own sentence
            raise BackendError(f"The brief could not be written to the shellbox: {e}",
                               retryable=False) from None
        command = (f"cd ~/{folder} && rm -f answer.md && agent {self.cli} -m "
                   f"'{self.model}' --minutes {minutes:g} -f brief.md -o answer.md")
        res = await self._run(shell, term, command)
        answer = await self._answer(f"{folder}/answer.md", shell.transport) or \
            (res.get("tail") or res.get("output") or "").strip()
        code = res.get("exit")
        metrics.AGENT_CLI_RUNS.labels(self.cli, "ok" if code in (0, None) else
                                      "timeout" if code == 124 else "error").inc()
        if code == 124:
            answer = f"{self.cli} ran out of its {minutes:g} minutes.\n\n{answer}"
        elif code not in (0, None):
            answer = f"{self.cli} ended with exit status {code}.\n\n{answer}"
        yield TextDelta(answer or f"{self.cli} said nothing.")
        yield Done(reason="stop")

    async def _run(self, shell, term: str, command: str) -> dict:
        """The run in its terminal until it ends; a Stop or the end of the turn press
        Ctrl-C there."""
        from ..errors import ToolRefused
        from ..skills.shell import RUN_MAX
        import httpx
        ended = False
        try:
            res = await shell._stream("/run", {"terminal": term, "command": command,
                                       "timeout": RUN_MAX}, _ignore)
            if res.get("state") == "busy":
                raise BackendError(f"Its terminal {term} is busy with another program.",
                                   retryable=False)
            if res.get("opened") and res.get("id"):      # it closes when this one is done
                from ..core.ids import now
                from ..terminals import store as terminals
                await terminals.upsert(self.services.store, str(res["id"]), term,
                                       "celine", now(), self.cid)
            while res.get("state") == "running":
                await anyio.sleep(POLL)
                _, res = await shell._post("/read", {"terminal": term, "from": 0}, read=60)
            ended = True
            return res
        except (ToolRefused, httpx.HTTPError) as e:
            ended = True
            raise BackendError(f"The shellbox did not run {self.cli}: {e}",
                               retryable=False) from None
        finally:
            if not ended:
                with anyio.CancelScope(shield=True), anyio.move_on_after(30):
                    try:
                        await shell._post("/stop", {"terminal": term}, read=20)
                    except Exception:       # noqa: BLE001 - the box is gone: nothing runs
                        pass

    async def _answer(self, rel: str, transport) -> str:
        from .. import box_files
        data = b""
        try:
            async for chunk in box_files.stream(self.services.cfg, rel, transport=transport):
                data += chunk
                if len(data) >= ANSWER_MAX:
                    break
        except (FileNotFoundError, RuntimeError):
            return ""
        return data[:ANSWER_MAX].decode(errors="replace").strip()


async def _ignore(_line: str) -> None:
    return None
