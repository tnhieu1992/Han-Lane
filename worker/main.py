"""advisory-worker: change stream on `events` (inserts only) -> commands to sim-bridge.

- Fast path (config [loop] fast_path): the gateway also POSTs each event here right after the
  insert; whichever path arrives first dispatches, the change stream stays the durable fallback.
- Keeps the resume token in a local file (no extra Atlas operations).
- Continues the trace injected by the gateway, with a span backdated to t3 that shows
  the change-stream wait.
- Never queries telemetry; writes `advisories` after sending, off the hot path.
"""
from __future__ import annotations

import json
import signal
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import httpx
from bson import json_util
from ddtrace.propagation.http import HTTPPropagator
from ddtrace.trace import tracer
from pymongo.errors import PyMongoError

from common.logging import setup
from common.metrics import statsd
from common.mongo import get_db
from common.settings import ROOT, config, settings
from common.timeutil import now_ns, ns_to_dt

log = setup("hanlane-worker")
TOKEN_FILE = ROOT / "worker" / ".resume_token"
PIPELINE = [{"$match": {"operationType": "insert"}}]


class Worker:
    def __init__(self) -> None:
        self.db = get_db("hanlane-worker")
        self.sim = httpx.Client(base_url=settings().sim_control_url, timeout=2.0)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="advisories-writer")
        self.running = True
        self.handled = 0
        self.lock = threading.Lock()
        self.dispatched: OrderedDict[str, None] = OrderedDict()
        self.stale_after_ms = float(config().get("loop", {}).get("stale_after_ms", 3000))

    def serve_fast_path(self, port: int) -> None:
        """HTTP endpoint the gateway calls right after inserting an event (fast path)."""
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                t4 = now_ns()
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    doc = json_util.loads(self.rfile.read(length))
                except ValueError:
                    self.send_response(400)
                    self.end_headers()
                    return
                self.send_response(202)
                self.end_headers()
                worker.handle(doc, path="fast", t4=t4)

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

        httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, name="fast-path-http", daemon=True).start()
        log.info("fast path listening", extra={"port": port})

    def load_token(self):
        if TOKEN_FILE.exists():
            try:
                return json_util.loads(TOKEN_FILE.read_text())
            except (ValueError, json.JSONDecodeError):
                return None
        return None

    def save_token(self, token) -> None:
        tmp = TOKEN_FILE.with_suffix(".tmp")
        tmp.write_text(json_util.dumps(token))
        tmp.replace(TOKEN_FILE)

    def run(self) -> None:
        token = self.load_token()
        log.info("worker starting", extra={"resume": token is not None})
        while self.running:
            try:
                with self.db.events.watch(PIPELINE, resume_after=token, max_await_time_ms=500) as stream:
                    while self.running:
                        change = stream.try_next()
                        if change is None:
                            continue
                        self.handle(change["fullDocument"], path="changestream", t4=now_ns())
                        token = stream.resume_token
                        self.save_token(token)
            except PyMongoError as e:
                if "resume" in str(e).lower() or "ChangeStreamHistoryLost" in str(e):
                    log.warning("resume token unusable, starting from now", extra={"error": str(e)})
                    token = None
                    TOKEN_FILE.unlink(missing_ok=True)
                else:
                    log.warning("change stream error, retrying", extra={"error": str(e)})
                time.sleep(1.0)

    def handle(self, doc: dict, path: str, t4: int) -> None:
        """Dispatch one event. path is "fast" (gateway HTTP) or "changestream".
        Whichever path delivers an event first dispatches it; the other only records its lag."""
        event_id = str(doc["_id"])
        etype = doc.get("type", "UNKNOWN")
        timing = doc.get("timing", {})
        t3 = timing.get("t3")
        parent = HTTPPropagator.extract(doc.get("trace") or {})

        # Span covering t3 -> t4 for this path (backdated to t3).
        wait = tracer.start_span(f"{path}.wait", child_of=parent, service="hanlane-worker", resource=etype)
        if t3:
            wait.start_ns = int(t3)
        wait.finish(finish_time=t4 / 1e9)
        if path == "changestream" and t3:
            statsd().distribution("changestream.lag_ms", (t4 - t3) / 1e6, tags=[f"event_type:{etype.lower()}"])

        with self.lock:
            if event_id in self.dispatched:
                return
            self.dispatched[event_id] = None
            while len(self.dispatched) > 5000:
                self.dispatched.popitem(last=False)

        # A warning that arrives seconds late is useless or harmful (the vehicle has moved on).
        # Happens when the change stream replays events after a worker restart.
        if t3 and (t4 - t3) / 1e6 > self.stale_after_ms:
            statsd().increment("advisory.stale", tags=[f"event_type:{etype.lower()}", f"path:{path}"])
            if etype != "PROBE":
                log.warning("stale event dropped", extra={"event_id": event_id, "event_type": etype,
                                                          "age_ms": round((t4 - t3) / 1e6), "path": path})
            return

        with tracer.start_span("worker.dispatch", child_of=parent, service="hanlane-worker", resource=etype,
                               activate=True) as span:
            recips = doc.get("recipients", [])
            span.set_tag("event.id", event_id)
            span.set_tag("event.recipients", len(recips))
            span.set_tag("dispatch.path", path)
            coords = (doc.get("loc") or {}).get("coordinates") or [None, None]
            payload = {
                "event_id": event_id, "event_type": etype, "recipients": recips, "path": path,
                "t0": timing.get("t0"), "t1": timing.get("t1"), "t3": t3, "t4": t4,
                "lon": coords[0], "lat": coords[1],
            }
            payload["t_send"] = now_ns()
            try:
                self.sim.post("/advisories", json=payload)
            except httpx.HTTPError as e:
                log.warning("send to sim-bridge failed", extra={"event_id": payload["event_id"], "error": str(e)})
                span.set_tag("error", True)
                return
            self.handled += 1
            if etype != "PROBE":
                for r in recips:
                    statsd().increment("advisory.sent", tags=[f"action:{r['action']}", f"event_type:{etype.lower()}"])
                # Logged inside the span so the line carries the incident's trace id.
                log.info("advisories sent", extra={
                    "event_id": payload["event_id"], "event_type": etype, "recipients": len(recips), "path": path,
                    "t3_t4_ms": round((t4 - t3) / 1e6, 1) if t3 else None,
                    "latitude": payload["lat"], "longitude": payload["lon"],
                })
                if recips:
                    self.pool.submit(self._write_advisories, doc, t4)

    def _write_advisories(self, doc: dict, t4: int) -> None:
        timing = doc.get("timing", {})
        now = ns_to_dt(now_ns())
        rows = [{
            "ts": now, "event_id": doc["_id"], "event_type": doc.get("type"), "vid": r["vid"], "action": r["action"],
            "t0": ns_to_dt(timing["t0"]) if timing.get("t0") else None,
            "t1": ns_to_dt(timing["t1"]) if timing.get("t1") else None,
            "t3": ns_to_dt(timing["t3"]) if timing.get("t3") else None,
            "t4": ns_to_dt(t4),
        } for r in doc.get("recipients", [])]
        try:
            self.db.advisories.insert_many(rows, ordered=False)
        except PyMongoError as e:
            log.warning("advisories write failed", extra={"error": str(e)})


def main() -> int:
    w = Worker()
    w.serve_fast_path(urlparse(settings().worker_url).port or 8200)

    def stop(*_):
        w.running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    w.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
