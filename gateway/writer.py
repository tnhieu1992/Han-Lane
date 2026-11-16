"""MongoDB writes off the request path.

Telemetry: one insert_many(ordered=False) per batch on a dedicated thread (WRITE_MODE=batch),
or one insert_one per record (WRITE_MODE=single, only to show the M0 ceiling).
Events: their own small pool so an incident never waits behind telemetry.
"""
from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
from bson import json_util
from pymongo.errors import PyMongoError

from common.metrics import statsd
from common.mongo import get_db
from common.timeutil import now_ns

log = logging.getLogger("hanlane-gateway.writer")


class TelemetryWriter:
    def __init__(self, mode: str, max_pending: int = 60):
        self.mode = mode
        self.q: queue.Queue = queue.Queue(maxsize=max_pending)
        self.coll = get_db("hanlane-gateway").telemetry
        self.dropped = 0
        self.last_write_ms: float | None = None
        threading.Thread(target=self._loop, name="telemetry-writer", daemon=True).start()

    def submit(self, docs: list[dict], t0: int) -> None:
        try:
            self.q.put_nowait((docs, t0))
        except queue.Full:
            try:
                self.q.get_nowait()
                self.dropped += 1
                statsd().increment("telemetry.dropped", tags=[f"write_mode:{self.mode}"])
            except queue.Empty:
                pass
            self.q.put_nowait((docs, t0))

    def pending(self) -> int:
        return self.q.qsize()

    def _loop(self) -> None:
        while True:
            docs, t0 = self.q.get()
            mode = self.mode
            start = now_ns()
            try:
                if mode == "single":
                    for d in docs:
                        self.coll.insert_one(d)
                else:
                    self.coll.insert_many(docs, ordered=False)
            except PyMongoError as e:
                log.warning("telemetry write failed", extra={"error": str(e), "write_mode": mode, "docs": len(docs)})
                statsd().increment("telemetry.write_errors", tags=[f"write_mode:{mode}"])
                continue
            t2 = now_ns()
            self.last_write_ms = (t2 - start) / 1e6
            sd = statsd()
            tags = [f"write_mode:{mode}"]
            sd.distribution("mongo.write_ms", self.last_write_ms, tags=tags)
            sd.distribution("ingest.lag_ms", (t2 - t0) / 1e6, tags=tags)
            sd.gauge("mongo.pending_batches", self.q.qsize(), tags=tags)


class EventWriter:
    def __init__(self):
        self.coll = get_db("hanlane-gateway").events
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="events-writer")

    def insert(self, doc: dict) -> None:
        self.pool.submit(self._insert, doc)

    def close(self, event_id, fields: dict) -> None:
        self.pool.submit(self._close, event_id, fields)

    def _insert(self, doc: dict) -> None:
        try:
            self.coll.insert_one(doc)
        except PyMongoError as e:
            log.error("event insert failed", extra={"error": str(e), "event_type": doc.get("type")})

    def _close(self, event_id, fields: dict) -> None:
        try:
            self.coll.update_one({"_id": event_id}, {"$set": {"status": "closed", **fields}})
        except PyMongoError as e:
            log.warning("event close failed", extra={"error": str(e)})


class FastPath:
    """POST each event straight to the worker (in parallel with the events insert)."""

    def __init__(self, worker_url: str):
        self.client = httpx.Client(base_url=worker_url, timeout=1.0)
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fast-path")
        self.failures = 0

    def send(self, doc: dict) -> None:
        self.pool.submit(self._send, doc)

    def _send(self, doc: dict) -> None:
        try:
            self.client.post("/dispatch", content=json_util.dumps(doc), headers={"Content-Type": "application/json"})
        except Exception as e:  # noqa: BLE001 - the change stream still delivers the event
            self.failures += 1
            if self.failures % 50 == 1:
                log.warning("fast path send failed, change stream will deliver", extra={"error": str(e)})
