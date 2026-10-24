"""Numbers for Prometheus and Grafana.

The app counts what it already passes through: the dispatcher a turn's end, the tool
chokepoint a call's state, the model queue a model call, the schedules a run's end, the
/llm/v1 proxy a forwarded call, the sub-agents their briefs and reports, the guard its
decisions (record_guard), the gateway its hosts and the pause, the work queue its
items. Nothing here asks anything; it only counts.

They are served on their own port, `[server] metrics_port` (CELINE_METRICS_PORT; the
image sets 8772), never on the app's port. That is how the app tells internal from
external: the edge router and the reverse proxy only ever reach the app's port, the
metrics port listens on loopback only, and a request that a proxy forwarded (it carries
X-Forwarded-For, Forwarded or X-Real-IP) is refused there all the same. No port set:
no listener, and the counters only count.
"""
from __future__ import annotations

import asyncio
import logging
import re

from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Gauge, Histogram,
                               disable_created_metrics, generate_latest)

log = logging.getLogger("celine.metrics")
disable_created_metrics()       # no `_created` twin beside every series

_SECONDS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1200, 1800, 3600)
_FAST = (0.05, 0.1, 0.25, 0.5, 1, 1.5, 2, 3, 4, 6, 10, 20, 60, 120)

# ---- turns ----------------------------------------------------------------------------
TURNS_STARTED = Counter("celine_turns_started_total", "Turns a worker started",
                        ["origin"])
TURNS = Counter("celine_turns_total", "Turns ended, by how they ended (the turn state)",
                ["origin", "outcome"])
TURN_SECONDS = Histogram("celine_turn_seconds", "A turn from its start to its end",
                         ["origin"], buckets=_SECONDS)
TURN_STEPS = Histogram("celine_turn_steps", "Model requests (steps) per turn", ["origin"],
                       buckets=(1, 2, 3, 5, 8, 13, 21, 34, 55, 100, 200))
TOOL_CALLS = Counter("celine_tool_calls_total", "Tool calls by tool and state "
                     "(ok, error, gated is not run, cancelled, detached)", ["tool", "state"])
TOOL_SECONDS = Histogram("celine_tool_seconds", "A tool call run in the turn",
                         ["tool"], buckets=_FAST)

# ---- models ---------------------------------------------------------------------------
MODEL_REQUESTS = Counter("celine_model_requests_total", "Model calls by outcome "
                         "(ok, error, cancelled)", ["role", "provider", "model", "outcome"])
MODEL_FIRST_TOKEN = Histogram("celine_model_first_token_seconds",
                              "From the slot to the first event (prompt read, load)",
                              ["role", "provider", "model"], buckets=_FAST)
MODEL_SECONDS = Histogram("celine_model_seconds", "From the slot to the last event",
                          ["role", "provider", "model"], buckets=_SECONDS)
MODEL_TOKENS = Counter("celine_model_tokens_total", "Tokens the provider counted",
                       ["role", "provider", "model", "direction"])
QUEUE_WAIT = Histogram("celine_model_queue_wait_seconds", "Waiting for a slot in the queue",
                       ["role", "provider"], buckets=_FAST)
MODEL_SWAPS = Counter("celine_model_swaps_total", "A request that made llama-swap load "
                      "another model", ["provider", "model"])

# ---- routines, shellbox, sub-agents ---------------------------------------------------
ROUTINE_RUNS = Counter("celine_routine_runs_total", "Routine runs ended, by state "
                       "(done, failed, skipped, stopped)", ["schedule", "state"])
ROUTINE_SECONDS = Histogram("celine_routine_seconds", "A routine run from its start to "
                            "its end", ["schedule"], buckets=_SECONDS)
PROXY_REQUESTS = Counter("celine_llm_proxy_requests_total", "Calls of the agent's CLIs "
                         "through /llm/v1, by the provider's status", ["model", "status"])
PROXY_TOKENS = Counter("celine_llm_proxy_tokens_total", "Tokens of /llm/v1 calls, as "
                       "the answer counted them", ["model", "direction"])
SUB_AGENTS_STARTED = Counter("celine_sub_agents_started_total", "Briefs given to "
                             "sub-agents (kind model or cli)", ["kind"])
SUB_AGENTS_FINISHED = Counter("celine_sub_agents_finished_total", "Sub-agent reports, by "
                              "the state of its last turn", ["state"])
AGENT_CLI_RUNS = Counter("celine_agent_cli_runs_total", "Agent CLI runs in the shellbox",
                         ["cli", "outcome"])

# ---- the guard ------------------------------------------------------------------------
GUARD_DECISIONS = Counter("celine_guard_decisions_total", "Bastet's decisions (allow, "
                          "would_stop, stop, found, passed, failed)",
                          ["mode", "verdict", "source"])
GUARD_JUDGE = Histogram("celine_guard_judge_seconds", "The judge's time per judged call",
                        buckets=_FAST)
GUARD_WAIT = Histogram("celine_guard_wait_seconds", "Time a call waited for the judge "
                       "to answer at all", buckets=_SECONDS)

# ---- the gateway and the work queue ---------------------------------------------------
HOST_UP = Gauge("celine_host_up", "A model provider's host answers its ping (1) or not (0)",
                ["provider"])
PAUSED = Gauge("celine_paused", "The agent is paused: no host of the chat and tasks models "
               "answers (1)")
WORK = Counter("celine_work_items_total", "Work queue items run, by outcome (done, "
               "deferred, queued, failed)", ["kind", "outcome"])


def origin_of(origin: str | None, kind: str | None) -> str:
    """A turn's origin as the dashboards read it: owner, intercom, routine, mission,
    sub_agent."""
    by_kind = {"background": "routine", "mission": "mission", "agent": "sub_agent"}
    if kind in by_kind:
        return by_kind[kind]
    return "owner" if (origin or "user") == "user" else "intercom"


def record_guard(decision: str, source: str, ms: float, *, mode: str,
                 waited_ms: float = 0.0) -> None:
    """One decision of Bastet's (guard.check). `decision`: a row's decision;
    `source`: the rule (every rule but hijack), or for hijack where the outside text came
    from, kept short (a skill, a domain or a first
    word: `browser__open`, `reddit.com/r/x` and `document a.pdf` count as browser,
    reddit.com and document); `ms`: the judge's time, 0 when no judge was asked;
    `waited_ms`: how long the call waited for the judge to come back."""
    src = (source or "none").split("__")[0].split("/")[0].split(" ")[0][:40] or "none"
    GUARD_DECISIONS.labels(mode, decision, src).inc()
    if ms > 0:
        GUARD_JUDGE.observe(ms / 1000)
    if waited_ms > 0:
        GUARD_WAIT.observe(waited_ms / 1000)


_USAGE = re.compile(rb'"(prompt_tokens|input_tokens|completion_tokens|output_tokens)"'
                    rb'\s*:\s*(\d+)')
_SIDE = {b"prompt_tokens": "in", b"input_tokens": "in",
         b"completion_tokens": "out", b"output_tokens": "out"}


def usage_in(raw: bytes) -> dict[str, int]:
    """Tokens in and out from the start and end of an OpenAI or Anthropic answer, JSON
    or SSE: the largest count seen per side (Anthropic counts up as it streams)."""
    out = {"in": 0, "out": 0}
    for name, n in _USAGE.findall(raw):
        side = _SIDE[name]
        out[side] = max(out[side], int(n))
    return out


# ---- the listener ---------------------------------------------------------------------

_PROXIED = {"x-forwarded-for", "forwarded", "x-real-ip"}


def _reply(status: str, body: bytes, ctype: str = "text/plain; charset=utf-8") -> bytes:
    return (f"HTTP/1.1 {status}\r\ncontent-type: {ctype}\r\ncontent-length: {len(body)}"
            f"\r\nconnection: close\r\n\r\n").encode() + body


def answer(head: bytes) -> bytes:
    """The reply to one request head."""
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split()
    names = {ln.split(":", 1)[0].strip().lower() for ln in lines[1:] if ":" in ln}
    if names & _PROXIED:
        return _reply("403 Forbidden", b"metrics are internal\n")
    if len(parts) < 2 or parts[0] != "GET" or parts[1].split("?")[0] != "/metrics":
        return _reply("404 Not Found", b"only GET /metrics\n")
    return _reply("200 OK", generate_latest(), CONTENT_TYPE_LATEST)


async def _answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        writer.write(answer(head))
        await writer.drain()
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
            ConnectionError):
        pass
    finally:
        writer.close()


async def serve(port: int) -> None:
    """Runs until cancelled. Loopback only, like the health port: the boxes share a
    network with the app and must not read it."""
    server = await asyncio.start_server(_answer, "127.0.0.1", port)
    log.info("metrics listener up", extra={"event": "metrics.listening", "port": port})
    async with server:
        await server.serve_forever()
