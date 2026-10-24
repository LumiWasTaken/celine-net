"""The embedding client and the vector formats of the semantic index.

- `embed_sync(s, texts)`: one OpenAI-compatible `/embeddings` call, with retries and
  a shrinking input limit when the server says an input is too long.
- `Pool`: several batches in flight on one endpoint.
- `fingerprint(s)`: the vectors of three fixed texts (PROBES). An index stores the
  fingerprint of the model that made it, and a model may add to or search an index
  only when its own fingerprint matches: cosine of at least MATCH on every probe.
  Measured: the same GGUF under another server or name gives 0.9996 to 0.9998; a
  Q4_K_M build of the same model 0.985 to 0.994, and mixing those vectors changes 21 %
  of a top 20, so it counts as another model. An index is identified by what the model
  produces, not by its name.
- `Vec`: a vector cut to the first `dims` dimensions (Qwen3-Embedding is Matryoshka
  trained, so the leading dimensions carry most of it), L2-normalised, and stored two
  ways: int8 with a per-vector scale (cosine does not care about scale) for scoring,
  and one sign bit per dimension for a fast Hamming pre-filter.
- The queue: a build's batch takes a BACKGROUND ticket on the embedding endpoint in the
  app's LLM queue (`use_queue`, labelled BUILD_LABEL), so a request someone waits for on
  the same server goes first and its wait names the build.
"""
from __future__ import annotations

import asyncio
import concurrent.futures as cf
import logging
import math
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import httpx

log = logging.getLogger("celine.kb.embed")


class SemanticError(Exception):
    pass


class InputTooLong(SemanticError):
    """The server refused an input for its length."""


# what servers say when an input is over their context (llama.cpp, OpenAI, vLLM, TEI)
_TOO_LONG = ("exceed", "too large", "too long", "context length", "context size", "truncat")


# ---- one endpoint ----------------------------------------------------------------------

def embed_sync(s: dict[str, Any], texts: list[str]) -> list[list[float]]:
    """Embed a batch. Inputs longer than `max_input_chars` are cut; a server that still
    says the input exceeds its context gets the batch again at half the length, down
    to 400 characters, so one long message never stops a whole build."""
    limit = int(s.get("max_input_chars", 3000))
    while True:
        try:
            vecs = _embed_once(s, [t[:limit] for t in texts])
        except InputTooLong:
            if limit > 400:
                limit //= 2
                continue
            raise
        if len(vecs) != len(texts):
            raise SemanticError(f"the embedding server returned {len(vecs)} vectors for "
                                f"{len(texts)} inputs")
        return vecs


def _embed_once(s: dict[str, Any], texts: list[str]) -> list[list[float]]:
    headers = {**s.get("headers", {})}
    if s.get("key"):
        headers["Authorization"] = f"Bearer {s['key']}"
    tries = max(1, int(s.get("retries", 4)))      # a build passes more: a model swap takes a while
    for attempt in range(tries):
        try:
            r = httpx.post(s["url"], json={"model": s["model"], "input": texts},
                           headers=headers,
                           timeout=httpx.Timeout(float(s.get("timeout", 300.0))))
        except httpx.HTTPError as e:
            if attempt == tries - 1:
                raise SemanticError(_no_answer(s, e)) from None
            time.sleep(min(2 ** attempt, 30))
            continue
        if r.status_code >= 500 and attempt < tries - 1:
            time.sleep(min(2 ** attempt, 30))
            continue
        if r.status_code in (401, 403):
            raise SemanticError(f"the embedding server at {_host(s)} rejected the API key "
                                f"(Settings, Models)")
        if r.status_code == 404:
            raise SemanticError(f"the embedding server at {_host(s)} has no model "
                                f"{s.get('model')} (Settings, Models)")
        if r.status_code >= 400:
            msg = f"the embedding server at {_host(s)} answered {r.status_code}"
            if r.status_code in (400, 413, 422) and any(w in r.text.lower() for w in _TOO_LONG):
                raise InputTooLong(f"{msg}: {r.text[:200]}")
            raise SemanticError(msg)
        try:
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            return [d["embedding"] for d in data]
        except (ValueError, KeyError, TypeError):
            raise SemanticError(f"the embedding server at {_host(s)} answered without "
                                f"vectors: is {s.get('url')} an /embeddings endpoint?") from None
    raise SemanticError(f"the embedding server at {_host(s)} kept failing")


def _host(s: dict[str, Any]) -> str:
    from urllib.parse import urlsplit
    return urlsplit(str(s.get("url", ""))).netloc or str(s.get("url", ""))


def _no_answer(s: dict[str, Any], e: httpx.HTTPError) -> str:
    if isinstance(e, httpx.TimeoutException):
        return (f"the embedding server at {_host(s)} did not answer within "
                f"{float(s.get('timeout', 300.0)):g} s")
    return f"the embedding server at {_host(s)} refused the connection (Settings, Models)"


# ---- vector formats --------------------------------------------------------------------

@dataclass(frozen=True)
class Vec:
    int8: bytes          # dims signed bytes, per-vector scale
    bits: bytes          # dims / 8 bytes, bit i = dimension i > 0
    fp: tuple | None = None   # the fingerprint of the model that made it (queries)

    @property
    def dims(self) -> int:
        return len(self.int8)


def truncate(v: Sequence[float], dims: int) -> list[float]:
    """First `dims` dimensions, L2-normalised."""
    v = list(v[:dims])
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def quantize(v: Sequence[float], dims: int, fp: tuple | None = None) -> Vec:
    u = truncate(v, dims)
    peak = max((abs(x) for x in u), default=0.0) or 1.0
    q = [max(-127, min(127, round(x * 127 / peak))) for x in u]
    bits = bytearray(len(u) // 8)
    for i, x in enumerate(u[: len(bits) * 8]):
        if x > 0:
            bits[i >> 3] |= 1 << (i & 7)
    return Vec(int8=struct.pack(f"{len(q)}b", *q), bits=bytes(bits), fp=fp)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


# ---- fingerprints ------------------------------------------------------------------------

PROBES = ["hey, are you coming tonight?", "ich hab heute endlich die Küche aufgeräumt",
          "WhatsApp group \"Test\"\nme, A\nTuesday 14 November 2023, evening\n"
          "A: chess later?\nme: server lags again"]
MATCH = 0.999
_fingerprints: dict[tuple, tuple] = {}
_fp_lock = threading.Lock()


def fingerprint(s: dict[str, Any], embed: Callable[[dict[str, Any], list[str]],
                                                   list[list[float]]] | None = None) -> tuple:
    """The PROBES' vectors from this endpoint (full width, L2-normalised), asked once
    per endpoint and model in this process. `embed` defaults to embed_sync."""
    key = (s.get("url"), s.get("model"), bool(s.get("key")))
    with _fp_lock:
        hit = _fingerprints.get(key)
    if hit is not None:
        return hit
    vecs = (embed or embed_sync)({**s, "retries": 1}, PROBES)
    fp = tuple(tuple(truncate(v, len(v))) for v in vecs)
    with _fp_lock:
        _fingerprints[key] = fp
    return fp


def stored_probes(fp: tuple, dims: int) -> list[list[float]]:
    """What an index keeps of a fingerprint: each probe cut to the index width."""
    return [[round(x, 6) for x in truncate(v, dims)] for v in fp]


def matches(stored: Sequence[Sequence[float]] | None, fp: tuple | None) -> bool:
    """Does a model with fingerprint `fp` give the vectors the index was made with?"""
    if not stored or fp is None or len(stored) != len(fp):
        return False
    dims = len(stored[0])
    if any(len(v) < dims for v in fp):
        return False
    return min(cosine(a, truncate(b, dims)) for a, b in zip(stored, fp)) >= MATCH


def forget_fingerprints(base_url: str | None = None) -> None:
    """Ask again next time: every endpoint, or the ones under `base_url` (a provider
    that came back or was checked again may serve other weights now)."""
    with _fp_lock:
        for k in list(_fingerprints):
            if base_url is None or str(k[0] or "").startswith(base_url.rstrip("/")):
                del _fingerprints[k]


# ---- sharing the embedding server -------------------------------------------------------

BUILD_LABEL = "Search index build"
_queue: tuple[Any, Any] | None = None   # (backends/queue.py Scheduler, its event loop)


def use_queue(scheduler, loop) -> None:
    """Builds take their tickets in `scheduler`, the app's LLM queue on `loop`."""
    global _queue
    _queue = None if scheduler is None else (scheduler, loop)


def _batch(fn, s: dict[str, Any], texts: list[str]) -> list[list[float]]:
    """One build batch: a BACKGROUND ticket on the endpoint first, when the queue is set."""
    ticket = None
    if _queue is not None:
        from ..backends.queue import Priority, Ticket
        sched, loop = _queue
        endpoint = str(s.get("url", "")).removesuffix("/embeddings").rstrip("/")

        async def take():              # made on the loop: a ticket's event lives there
            t = Ticket(endpoint=endpoint, model=str(s.get("model", "")),
                       priority=Priority.BACKGROUND, lane="index-build", label=BUILD_LABEL)
            return await sched.acquire(t)
        ticket = asyncio.run_coroutine_threadsafe(take(), loop).result()
    try:
        return fn(s, texts)
    finally:
        if ticket is not None:
            _queue[1].call_soon_threadsafe(_queue[0].release, ticket)


class Pool:
    """Batches in flight on one endpoint: `workers` requests at once. `fn(settings,
    texts)` does one request; tests pass a fake."""

    def __init__(self, s: dict[str, Any], fn: Callable[[dict[str, Any], list[str]],
                                                          list[list[float]]]):
        self.fn = fn
        self._s = s
        self.workers = max(1, int(s.get("workers", 4)))

    def run(self, batches: list[list[str]],
            on_batch: Callable[[int, list[list[float]]], None],
            check: Callable[[], None]) -> None:
        """Embed every batch; `on_batch(index, vectors)` runs in the caller's thread,
        in completion order. `check()` raises to stop (a cancelled task)."""
        with cf.ThreadPoolExecutor(self.workers, thread_name_prefix="embed") as ex:
            pending: set[cf.Future] = set()
            it = iter(enumerate(batches))
            done = False
            while pending or not done:
                while not done and len(pending) < self.workers:
                    nxt = next(it, None)
                    if nxt is None:
                        done = True
                        break
                    i, texts = nxt
                    pending.add(ex.submit(lambda i=i, t=texts: (i, _batch(self.fn, self._s, t))))
                if not pending:
                    break
                finished, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
                try:
                    check()
                    for f in finished:
                        i, vecs = f.result()
                        on_batch(i, vecs)
                except BaseException:
                    for f in pending:
                        f.cancel()
                    raise
