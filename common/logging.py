"""JSON logging to stdout and logs/<service>.log, with Datadog trace correlation.

ddtrace (DD_LOGS_INJECTION=true) adds dd.trace_id / dd.span_id to each LogRecord;
the formatter copies them into the JSON line so logs link to traces.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from common.settings import ROOT

_DD_ATTRS = ("dd.trace_id", "dd.span_id", "dd.service", "dd.env", "dd.version")
_STD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for attr in _DD_ATTRS:
            val = getattr(record, attr, None)
            if val not in (None, "", "0"):
                out[attr] = val
        for key, val in record.__dict__.items():
            if key not in _STD_ATTRS and key not in _DD_ATTRS and not key.startswith("_"):
                out[key] = val
        if record.exc_info:
            out["error.stack"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup(service: str, level: int = logging.INFO) -> logging.Logger:
    root = logging.getLogger()
    if getattr(root, "_hanlane_configured", False):
        return logging.getLogger(service)
    root.setLevel(level)
    fmt = JsonFormatter()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    if os.environ.get("HANLANE_LOG_FILE", "1") == "1":
        log_dir = ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        fh = logging.FileHandler(log_dir / f"{service}.log")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "uvicorn.access", "pymongo"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._hanlane_configured = True  # type: ignore[attr-defined]
    return logging.getLogger(service)
