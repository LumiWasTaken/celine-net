#!/usr/bin/env python3
"""agent: the agent CLIs of its shellbox, each run headless on one task with its models.

    agent list                                  the CLIs, and the models it may use
    agent <cli> [-m MODEL] [-o FILE] [--minutes N] [-f TASK_FILE | TASK...]

A run works in the current folder, prints what the CLI prints, then its final answer
after a line "==== answer", and writes that answer to FILE with -o. The models come
from the app's model proxy ($OPENAI_BASE_URL with $OPENAI_API_KEY, set in every terminal
by shellboxd); MODEL is one of its own, `default` when left out. opencode runs on the config
the box wrote for it (~/.config/opencode/opencode.json, shellbox/agents/opencode.py: every
model of its own with its real limits) and its own opencode.jsonc. --minutes ends the run
after that many minutes (exit 124). The exit status is the CLI's.

Standard library only: the app's CLI sub-agents (server/celine/sub_agents/cli.py) run
this same command, so it and they run a CLI the same way.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

ANSWER_MARK = "==== answer"
TAIL = 4000         # characters of the output that are the answer when a CLI gives no other


def _json_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _opencode_answer(text: str) -> str | None:
    parts = [e["part"]["text"] for e in _json_lines(text)
             if e.get("type") == "text" and isinstance(e.get("part"), dict)
             and e["part"].get("text")]
    return parts[-1] if parts else None


def command(cli: str, model: str, task: str) -> tuple[list[str], object]:
    """(argv, answer reader) of one headless run."""
    if cli == "opencode":
        return (["opencode", "run", "--auto", "--format", "json", "-m", f"celine/{model}",
                 task], _opencode_answer)
    raise KeyError(cli)


CLIS = {
    "opencode": "opencode: reads, edits and runs code in a project",
}


def models() -> list[tuple[str, str]]:
    req = urllib.request.Request(os.environ.get("OPENAI_BASE_URL", "") + "/models", headers={
        "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return [(m["id"], m.get("name", "")) for m in json.load(r)["data"]]


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("list", "-h", "--help"):
        print("CLIs, run with: agent <cli> [-m MODEL] [-o FILE] [--minutes N] TASK")
        for name, what in CLIS.items():
            print(f"  {name:<10} {what}")
        if not os.environ.get("OPENAI_BASE_URL"):
            print("Your models: OPENAI_BASE_URL is not set in this shell.")
            return 0
        try:
            print("Your models (-m):")
            for ref, name in models():
                print(f"  {ref:<32} {name}")
        except Exception as e:                              # noqa: BLE001
            print(f"Your models: the model proxy did not answer ({type(e).__name__}: {e}).")
        return 0
    ap = argparse.ArgumentParser(prog="agent", description="Run one agent CLI on a task.")
    ap.add_argument("cli", choices=sorted(CLIS))
    ap.add_argument("-m", "--model", default="default")
    ap.add_argument("-o", "--output", help="write the final answer to this file")
    ap.add_argument("-f", "--task-file", help="read the task from this file")
    ap.add_argument("--minutes", type=float, help="end the run after this many minutes")
    ap.add_argument("task", nargs="*")
    a = ap.parse_args(argv)
    task = Path(a.task_file).read_text() if a.task_file else " ".join(a.task)
    if not task.strip():
        ap.error("give the task as words or with -f FILE")
    args, reader = command(a.cli, a.model, task)
    out: list[str] = []
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, errors="replace",
                            start_new_session=True)
    # the CLI and whatever it started end together: its own process group
    end = lambda sig=signal.SIGKILL: os.killpg(proc.pid, sig)  # noqa: E731
    timer = threading.Timer(a.minutes * 60, end) if a.minutes else None
    if timer:
        timer.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            out.append(line)
        rc = proc.wait()
    except KeyboardInterrupt:          # Ctrl-C in its terminal reaches this program
        end(signal.SIGTERM)
        rc = proc.wait()
    if timer and not timer.is_alive():
        rc = 124
        print(f"agent: ended after {a.minutes:g} minutes.")
    elif timer:
        timer.cancel()
    text = "".join(out)
    answer = reader(text) or text[-TAIL:].strip()
    print(f"\n{ANSWER_MARK}\n{answer}")
    if a.output:
        Path(a.output).write_text(answer + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
