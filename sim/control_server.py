"""HTTP endpoint for sim-bridge. Requests go into a thread-safe queue; only the TraCI
thread touches SUMO (TraCI allows a single controlling thread)."""
from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from ddtrace.propagation.http import HTTPPropagator

from common.timeutil import now_ns

log = logging.getLogger("hanlane-sim.control")


@dataclass
class Command:
    kind: str                     # "advisory" | "control"
    payload: dict
    received_ns: int
    trace_ctx: Any = None
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None


class ControlServer:
    def __init__(self, port: int, commands: "queue.Queue[Command]", status: Callable[[], dict]):
        self.commands = commands
        self.status = status
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence default access log
                pass

            def _reply(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path in ("/healthz", "/status"):
                    self._reply(200, server.status())
                else:
                    self._reply(404, {"error": "not found"})

            def do_POST(self):
                t_recv = now_ns()
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self._reply(400, {"error": "bad json"})
                    return
                ctx = HTTPPropagator.extract({k.lower(): v for k, v in self.headers.items()})
                if self.path == "/advisories":
                    server.commands.put(Command("advisory", payload, t_recv, ctx))
                    self._reply(202, {"queued": True})
                elif self.path == "/control":
                    cmd = Command("control", payload, t_recv, ctx)
                    server.commands.put(cmd)
                    if cmd.done.wait(timeout=3.0):
                        self._reply(200, cmd.result or {})
                    else:
                        self._reply(202, {"queued": True})
                else:
                    self._reply(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self.httpd.daemon_threads = True

    def start(self) -> None:
        threading.Thread(target=self.httpd.serve_forever, name="control-http", daemon=True).start()
        log.info("control server listening", extra={"port": self.httpd.server_address[1]})

    def stop(self) -> None:
        self.httpd.shutdown()
