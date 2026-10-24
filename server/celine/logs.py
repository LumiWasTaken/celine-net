"""Structured logging, with redaction at the sink."""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

from .core.ids import iso
from .errors import redact

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message", "asctime", "taskName",
}


# Redaction happens here, at the sink, so a stray logger.debug(f"{request}")
# cannot leak a key.
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            # formatTime() has no %f and uses localtime, so it would emit local time with a Z
            "ts": iso(datetime.fromtimestamp(record.created, timezone.utc)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                out[k] = redact(v) if isinstance(v, str) else v
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extra = " ".join(
            f"{k}={v}" for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_") and k != "event"
        )
        event = getattr(record, "event", "")
        head = f"{record.levelname[0]} {event or record.name}"
        line = f"{head:<28} {redact(record.getMessage())}"
        if extra:
            line += f"  {redact(extra)}"
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        return line


def setup(log_dir: Path, dev: bool, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "celine.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(HumanFormatter() if dev else JsonFormatter())
    root.addHandler(stderr)

    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
