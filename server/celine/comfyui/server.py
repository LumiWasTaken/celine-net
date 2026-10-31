"""A ComfyUI server: the few endpoints the app uses, one prompt run to its end, the
presets it can run (its "models" in Settings), and the startup free."""
from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import anyio
import httpx

from ..settings.registry import provider_headers
from . import presets as presets_mod
from .presets import ImageError

log = logging.getLogger("celine.comfyui")

POLL = 1.0                  # seconds between /history looks
REQUEST = 30.0              # seconds one request to ComfyUI may take
PROBE = 2.5                 # seconds the Settings check waits for ComfyUI
CLEANUP_SECONDS = 5.0       # the most the interrupt and /free may take after a Stop; a
                            # restart frees what was left (free_shared)


class Comfy:
    """The few ComfyUI endpoints the app uses."""

    def __init__(self, base_url: str, headers: dict[str, str] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=REQUEST, transport=transport,
                                        headers=headers or {})

    async def close(self) -> None:
        await self.client.aclose()

    async def object_info(self, classes: set[str]) -> dict[str, Any]:
        try:
            return await object_info(self.client, self.url, classes)
        except ValueError:
            raise ImageError(f"ComfyUI at {_host(self.url)} answered /object_info with "
                             f"something that is not JSON: is {self.url} a ComfyUI "
                             f"server?") from None

    async def queue(self, graph: dict[str, Any], client_id: str) -> str:
        r = await self.client.post(f"{self.url}/prompt",
                                   json={"prompt": graph, "client_id": client_id})
        if r.status_code == 400:
            raise ImageError(_refused(r))
        r.raise_for_status()
        return str(r.json()["prompt_id"])

    async def upload(self, data: bytes, name: str, media_type: str) -> str:
        """The source of an edit, into ComfyUI's input folder. Returns the name its
        LoadImage node takes."""
        r = await self.client.post(f"{self.url}/upload/image",
                                   files={"image": (name, data, media_type)},
                                   data={"type": "input", "overwrite": "true"})
        r.raise_for_status()
        out = r.json() or {}
        return f"{out['subfolder']}/{out['name']}" if out.get("subfolder") else out["name"]

    async def history(self, prompt_id: str) -> dict[str, Any] | None:
        r = await self.client.get(f"{self.url}/history/{prompt_id}")
        r.raise_for_status()
        return (r.json() or {}).get(prompt_id)

    async def position(self, prompt_id: str) -> tuple[bool, int]:
        """(running now, prompts ahead of it in ComfyUI's own queue)."""
        r = await self.client.get(f"{self.url}/queue")
        r.raise_for_status()
        q = r.json() or {}
        if any(item[1] == prompt_id for item in q.get("queue_running") or []):
            return True, 0
        pending = sorted(q.get("queue_pending") or [], key=lambda item: item[0])
        ids = [item[1] for item in pending]
        return False, ids.index(prompt_id) if prompt_id in ids else 0

    async def output(self, ref: dict[str, Any]) -> tuple[bytes, str]:
        r = await self.client.get(f"{self.url}/view", params={
            "filename": ref["filename"], "subfolder": ref.get("subfolder", ""),
            "type": ref.get("type", "output")})
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "image/png").split(";")[0]

    async def stop(self, prompt_id: str) -> None:
        """Interrupts the prompt if it runs (only this one) and takes it out of the
        queue if it still waits there."""
        await self.client.post(f"{self.url}/interrupt", json={"prompt_id": prompt_id})
        await self.client.post(f"{self.url}/queue", json={"delete": [prompt_id]})

    async def free(self) -> None:
        await self.client.post(f"{self.url}/free",
                               json={"unload_models": True, "free_memory": True})


async def object_info(c: httpx.AsyncClient, url: str, classes: set[str],
                      headers: dict[str, str] | None = None) -> dict[str, Any]:
    """{class: its /object_info entry, or None when the server has no such node}.
    ValueError: an answer that is not JSON."""
    info: dict[str, Any] = {}
    for cls in sorted(classes):
        r = await c.get(f"{url.rstrip('/')}/object_info/{cls}", headers=headers)
        r.raise_for_status()
        info[cls] = (r.json() or {}).get(cls)
    return info


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    return urlsplit(url).netloc or url


def _refused(r: httpx.Response) -> str:
    """ComfyUI's 400 for a workflow it will not run, in one or two sentences."""
    try:
        data = r.json() or {}
    except ValueError:
        return "ComfyUI refused the workflow."
    why = _plain((data.get("error") or {}).get("message")) or "the workflow is not valid"
    out = f"ComfyUI refused the workflow: {why}."
    for node in (data.get("node_errors") or {}).values():
        for e in node.get("errors") or []:
            detail = _plain(e.get("details") or e.get("message"))
            if detail:
                return f"{out} {node.get('class_type', 'A node')}: {detail[:200]}."
    return out


def _failed(status: dict[str, Any]) -> str:
    for kind, data in status.get("messages") or []:
        if kind == "execution_error":
            why = _plain(data.get("exception_message"))[:300]
            return f"ComfyUI failed in {data.get('node_type', 'a node')}: {why}."
        if kind == "execution_interrupted":
            return "ComfyUI stopped the prompt before it was done."
    return "ComfyUI ended the prompt with an error."


def _plain(text: Any) -> str:
    """One line without a closing full stop, for a sentence of our own."""
    return " ".join(str(text or "").split()).rstrip(".")


def _output_ref(outputs: dict[str, Any]) -> dict[str, Any] | None:
    for out in outputs.values():
        for item in (out.get("images") or []) + (out.get("audio") or []):
            if isinstance(item, dict) and item.get("filename"):
                return item
    return None


async def discover(c: httpx.AsyncClient, url: str, headers: dict[str, str]) -> list[dict]:
    """Every preset, images and music, as a ComfyUI provider's models: [{id, name,
    caps, missing?}]. A preset whose node or file the server lacks says why."""
    found = [p for kind in presets_mod.FOLDERS for p in presets_mod.of(kind).values()]
    info = await object_info(c, url, {n["class_type"] for p in found
                                      for n in p.workflow.values()}, headers)
    out = []
    for p in found:
        entry: dict[str, Any] = {"id": p.id, "name": p.label, "caps": [p.role]}
        why = p.missing(info)
        if why:
            entry["missing"] = why
        out.append(entry)
    return out


async def free_shared(cfg, llm) -> int:
    """At startup: every ComfyUI provider that shares its GPU frees its models. A
    hard stop mid-batch (a kill, a power cut) left them loaded, and nothing else
    frees them until the next batch. Returns how many answered."""
    router = llm.router
    s = router.s
    n = 0
    for pid, p in (s.providers if s is not None else {}).items():
        shared = (p.get("options") or {}).get("shares_gpu_with")
        if p.get("kind") != "comfyui" or not shared:
            continue
        comfy = Comfy(p["base_url"], provider_headers(s, pid), router.transport)
        try:
            with anyio.fail_after(CLEANUP_SECONDS):
                await comfy.free()
            n += 1
        except Exception as e:
            log.info("comfyui not freed at startup", extra={
                "event": "comfyui.free_failed", "provider": pid, "error": type(e).__name__})
        finally:
            await comfy.close()
    if n:
        log.info("comfyui freed at startup", extra={"event": "comfyui.freed", "count": n})
    return n


async def run(comfy: Comfy, graph: dict[str, Any], note, timeout: float) -> tuple[bytes, str]:
    """Queue one prompt, follow it, fetch what it made. A prompt that did not finish
    (a Stop, an error, the timeout) is interrupted and taken out of ComfyUI's queue
    before this returns or raises. Freeing ComfyUI's memory is the batch's, at its end."""
    prompt_id: str | None = None
    finished = False
    try:
        await note("Sending the workflow to ComfyUI")
        prompt_id = await comfy.queue(graph, uuid4().hex)
        try:
            with anyio.fail_after(timeout):
                entry = await _follow(comfy, prompt_id, note)
        except TimeoutError:
            raise ImageError(f"ComfyUI did not finish within {timeout:.0f} s. The prompt "
                             f"was stopped.") from None
        finished = True
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            raise ImageError(_failed(status))
        ref = _output_ref(entry.get("outputs") or {})
        if ref is None:
            raise ImageError("The workflow finished without an output.")
        return await comfy.output(ref)
    finally:
        if prompt_id is not None and not finished:
            with anyio.CancelScope(shield=True), anyio.move_on_after(CLEANUP_SECONDS):
                try:
                    await comfy.stop(prompt_id)
                except httpx.HTTPError as e:
                    log.info("comfyui stop failed", extra={
                        "event": "comfyui.stop_failed", "error": type(e).__name__})


async def _follow(comfy: Comfy, prompt_id: str, note) -> dict[str, Any]:
    """Until the prompt is in /history as completed or failed; says where it is."""
    said = None
    while True:
        entry = await comfy.history(prompt_id)
        status = (entry or {}).get("status") or {}
        if entry is not None and (status.get("completed")
                                  or status.get("status_str") == "error"):
            return entry
        running, ahead = await comfy.position(prompt_id)
        line = ("Generating" if running else
                f"Waiting in the ComfyUI queue: {ahead} ahead" if ahead else
                "Waiting in the ComfyUI queue")
        if line != said:
            said = line
            await note(line)
        await anyio.sleep(POLL)


async def _probe(cfg, role: str) -> dict[str, str]:
    """Settings, Features (images, music): a ComfyUI provider is saved and answers
    /object_info; the detail says when the role has no preset yet."""
    s = cfg.settings
    comfy = [(pid, p) for pid, p in s.providers.items() if p.get("kind") == "comfyui"]
    if not comfy:
        return {"state": "unset", "detail": "Type the ComfyUI address below."}
    pid, p = comfy[0]
    url = str(p.get("base_url", "")).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=PROBE, headers=provider_headers(s, pid)) as c:
            (await c.get(f"{url}/object_info/CheckpointLoaderSimple")).raise_for_status()
    except httpx.HTTPError as e:
        from ..errors import her_unreachable, unreachable_words
        return {"state": "missing", "detail": unreachable_words("ComfyUI", url, e),
                "why": her_unreachable("ComfyUI", url)}
    from ..settings.registry import ROLES
    note = ("" if s.chain(role) else
            f" No preset is picked for {ROLES[role]['label']} yet (Settings, Images "
            f"& Music).")
    return {"state": "ready", "detail": f"ComfyUI at {_host(url)} answers.{note}"}


async def probe_images(cfg, services) -> dict[str, str]:
    return await _probe(cfg, "image_generation")


async def probe_music(cfg, services) -> dict[str, str]:
    return await _probe(cfg, "music_generation")
