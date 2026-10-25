"""Cross-encoder reranking through an OpenAI-style `/v1/rerank` endpoint (llama-server
`--reranking`, alone or behind llama-swap). The model is the first of the Reranking
task in Settings that can answer now (backends/router.py). A reranker that fails never
fails a search: the caller falls back to the fused order and says so."""
from __future__ import annotations

import time
from typing import Protocol

import httpx


class RerankError(Exception):
    pass


class Reranker(Protocol):
    def score(self, query: str, docs: list[str]) -> list[float]: ...


class HttpReranker:
    """One search's reranker (from_config makes one per search): `timeout` is the time
    all its calls together may take, so a slow server costs a search at most that,
    never the tool call's whole time limit."""

    def __init__(self, url: str, model: str, *, key: str | None = None,
                 timeout: float = 60.0, max_chars: int = 2000,
                 on_unreachable=None):
        self.url = url
        self.model = model
        self.key = key
        self.timeout = timeout
        self.max_chars = max_chars
        self.on_unreachable = on_unreachable
        self.deadline: float | None = None       # set by the first call

    def score(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        from ...backends.router import PROBE_TIMEOUT
        now = time.monotonic()
        if self.deadline is None:
            self.deadline = now + self.timeout
        left = self.deadline - now
        if left <= 0:
            raise RerankError(f"the reranker took over {self.timeout:g} s")
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        try:
            r = httpx.post(self.url, json={"model": self.model, "query": query,
                                           "documents": [d[: self.max_chars] for d in docs],
                                           "top_n": len(docs)},
                           headers=headers,
                           timeout=httpx.Timeout(left, connect=min(PROBE_TIMEOUT, left)))
        except httpx.HTTPError as e:
            # a server that is slow to rank is not unreachable; one that takes no
            # connection is, and the next search does not wait for it
            if self.on_unreachable is not None and \
                    isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
                self.on_unreachable(e)
            raise RerankError(f"the reranker did not answer: {e}") from None
        if r.status_code >= 400:
            raise RerankError(f"the reranker said {r.status_code}: {r.text[:200]}")
        out = [float("-inf")] * len(docs)
        for item in r.json().get("results", []):
            i = int(item["index"])
            if 0 <= i < len(docs):
                out[i] = float(item.get("relevance_score", item.get("score", 0.0)))
        return out


def reranker_available(cfg) -> bool:
    """A Reranking model is set (Settings, Models): the one gate for reranking."""
    from ...backends.router import app_router
    return bool(app_router(cfg).chain("reranker"))


def from_config(cfg) -> Reranker | None:
    """The first model of the Reranking task that can answer now; None when the task
    is off or none can (the search then says it is ranked by fusion)."""
    from ...backends.router import app_router
    router = app_router(cfg)
    ordered = router.order_sync("reranker")
    if not ordered.refs:
        return None
    ref = ordered.refs[0]
    b = router.backend_config(ref)
    pid = ref.partition("/")[0]
    return HttpReranker(f"{b.base_url}/rerank", b.model, key=b.api_key,
                        on_unreachable=lambda e: router.note_unreachable(
                            pid, router._unreachable_sentence(pid, e)))
