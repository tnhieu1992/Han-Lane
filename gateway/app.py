"""ingest-gateway.

POST /ingest      one batch per simulated second from sim-bridge: dedup, update RAM state,
                  run rules, write telemetry (insert_many, not awaited) and events (insert_one now).
GET  /ws          1 Hz snapshot for the web map (the web never queries Atlas).
/api/*            overview + control (incident, clear, advisories, scale, write mode) for the web.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from bson import ObjectId
from ddtrace.propagation.http import HTTPPropagator
from ddtrace.trace import tracer
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from common.logging import setup
from common.metrics import statsd
from common.mongo import get_client
from common.roadnet import load_net
from common.settings import config, settings
from common.timeutil import now_ns, ns_to_dt
from db import rollup
from gateway import recipients
from gateway.rules import Detection, RuleEngine
from gateway.state import LiveState
from gateway.writer import EventWriter, FastPath, TelemetryWriter

log = setup("hanlane-gateway")
CFG = config()
S = settings()
ADVISED_HOLD_S = 20.0


class Gateway:
    def __init__(self) -> None:
        self.net = load_net()
        self.lane_limits: dict[str, tuple[float, float]] = {}
        rules = CFG["rules"]
        self.state = LiveState(rules["stalled_speed"], rules["close_speed"])
        self.rules = RuleEngine(rules, self.lane_limit, self.lane_length)
        self.telemetry = TelemetryWriter(S.write_mode if S.write_mode in ("batch", "single") else "batch")
        self.events = EventWriter()
        self.fast = FastPath(S.worker_url) if CFG.get("loop", {}).get("fast_path") else None
        self.lock = threading.Lock()
        self.snapshot: dict = {}
        self.snapshot_version = 0
        self.feed: list[dict] = []          # recent events for the web panel
        self.sim = httpx.Client(base_url=S.sim_control_url, timeout=4.0)
        self.counts = {"events": {}, "batches": 0, "duplicates": 0, "updates": 0}
        self.notified: dict[ObjectId, set[str]] = {}   # open incident -> vids already warned

    def _lane_info(self, lane: str) -> tuple[float, float]:
        if lane not in self.lane_limits:
            try:
                ln = self.net.getLane(lane)
                self.lane_limits[lane] = (ln.getSpeed(), ln.getLength())
            except (KeyError, AttributeError):
                self.lane_limits[lane] = (0.0, 0.0)
        return self.lane_limits[lane]

    def lane_limit(self, lane: str) -> float | None:
        return self._lane_info(lane)[0] or None

    def lane_length(self, lane: str) -> float | None:
        return self._lane_info(lane)[1] or None

    # ----------------------------------------------------------------- ingest
    def ingest(self, batch: dict) -> dict:
        t1 = now_ns()
        with self.lock:
            if self.state.is_duplicate(batch["batch_id"]):
                self.counts["duplicates"] += 1
                return {"duplicate": True}
            self.counts["batches"] += 1
            t0, records = int(batch["t0"]), batch["records"]
            self.state.sim_stats = batch.get("stats") or {}
            self.state.apply_batch(float(batch["sim_time"]), records)

            # Telemetry: hand off to the writer thread right away, never awaited.
            ts, t0_dt = ns_to_dt(int(batch["ts"]) * 1_000_000), ns_to_dt(t0)
            docs = [self._telemetry_doc(r, ts, t0_dt) for r in records]
            self.telemetry.submit(docs, t0)
            sd = statsd()
            sd.distribution("mongo.batch_size", len(docs), tags=[f"write_mode:{self.telemetry.mode}"])
            by_type: dict[str, int] = {}
            for r in records:
                by_type[r["meta"]["type"]] = by_type.get(r["meta"]["type"], 0) + 1
            for vtype, n in by_type.items():
                sd.increment("telemetry.received", n, tags=[f"vehicle_type:{vtype}"])
            sd.gauge("vehicles.active", len(records), tags=["service:hanlane-gateway"])

            # Rules, then recipients, then events.insert_one immediately.
            fired = []
            for det in self.rules.evaluate(self.state):
                fired.append(self._fire(det, t0, t1, batch))
            for inc, reason in self.rules.closures(self.state):
                self.events.close(inc.event_id, {"closed_ts": ns_to_dt(now_ns()), "close_reason": reason,
                                                 "closed_sim_time": self.state.sim_time})
                self._push_feed({"kind": "closed", "type": inc.type, "event_id": str(inc.event_id),
                                 "vid": inc.vid, "reason": reason})
                log.info("event closed", extra={"event_id": str(inc.event_id), "event_type": inc.type,
                                                "reason": reason})
                self.notified.pop(inc.event_id, None)
            self._warn_newcomers(t0, t1)
            if batch.get("probe"):
                self._probe(batch, t0, t1)
            self._build_snapshot()
        return {"ok": True, "records": len(records), "fired": fired}

    @staticmethod
    def _telemetry_doc(r: dict, ts, t0_dt) -> dict:
        doc = {k: v for k, v in r.items() if k not in ("laneIndex", "leader")}
        doc["ts"], doc["t0"] = ts, t0_dt
        doc["loc"] = {"type": "Point", "coordinates": [r["lon"], r["lat"]]}
        return doc

    def _fire(self, det: Detection, t0: int, t1: int, batch: dict) -> dict:
        r = det.rec
        with tracer.trace("rules.fire", service="hanlane-gateway", resource=det.type) as span:
            recips = recipients.select(det, self.state, self.net, CFG["recipients"])
            t3 = now_ns()
            event_id = ObjectId()
            carrier: dict[str, str] = {}
            HTTPPropagator.inject(span.context, carrier)
            span.set_tag("event.id", str(event_id))
            span.set_tag("event.recipients", len(recips))
            doc = {
                "_id": event_id, "type": det.type, "status": "open",
                "ts": ns_to_dt(t3), "sensor_ts": ns_to_dt(t0),
                "loc": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "edge": r["edge"], "lane": r["lane"], "zone": r["zone"],
                "vid": r["meta"]["vid"] if det.type != "LANE_JAM" else None,
                "severity": det.severity, "details": det.details,
                "recipients": [{"vid": x["vid"], "action": x["action"], "params": x["params"]} for x in recips],
                "trace": carrier,
                "timing": {"t0": t0, "t1": t1, "t3": t3},
                "sim_time": self.state.sim_time,
                "expireAt": ns_to_dt(t3) + timedelta(seconds=CFG["ttl"]["events_seconds"]),
            }
            self.events.insert(doc)
            if self.fast:
                self.fast.send(doc)
        self.rules.opened(det, event_id, self.state.sim_time)
        self.notified[event_id] = {x["vid"] for x in recips}
        detect_ms = (t3 - t0) / 1e6
        tags = [f"event_type:{det.type.lower()}", f"zone:{r['zone']}"]
        statsd().increment("events.detected", tags=tags)
        statsd().distribution("detect.lag_ms", detect_ms, tags=tags[:1])
        until = time.time() + ADVISED_HOLD_S
        for x in recips:
            self.state.advised_until[x["vid"]] = until
        self.counts["events"][det.type] = self.counts["events"].get(det.type, 0) + 1
        info = {"event_id": str(event_id), "event_type": det.type, "vid": doc["vid"], "zone": r["zone"],
                "edge": r["edge"], "lane": r["lane"], "recipients": len(recips), "detect_lag_ms": round(detect_ms, 1),
                "latitude": r["lat"], "longitude": r["lon"], "details": det.details}
        log.info("event detected", extra=info)
        self._push_feed({"kind": "opened", "type": det.type, "event_id": str(event_id), "vid": doc["vid"],
                         "zone": r["zone"], "recipients": len(recips), "detect_lag_ms": round(detect_ms, 1),
                         "lon": r["lon"], "lat": r["lat"]})
        return {"event_id": str(event_id), "type": det.type, "recipients": len(recips)}

    def _warn_newcomers(self, t0: int, t1: int) -> None:
        """While a persistent hazard (STALLED, LANE_JAM) is open, warn vehicles that entered the
        150 m zone since the last notice. The rule does not fire again; this writes an `events`
        document with `update_of` pointing at the open incident, so the worker path is unchanged."""
        for inc in list(self.rules.open.values()):
            if inc.type not in ("STALLED", "LANE_JAM") or inc.event_id not in self.notified:
                continue
            if inc.vid:
                v = self.state.vehicles.get(inc.vid)
                if v is None:
                    continue
                rec = v.rec
            else:
                lane_recs = [r for r in self.state.by_edge.get(inc.lane.rsplit("_", 1)[0], []) if r["lane"] == inc.lane]
                if not lane_recs:
                    continue
                rec = min(lane_recs, key=lambda r: r["pos"])
            det = Detection(inc.type, inc.key, rec, "update")
            new = [x for x in recipients.select(det, self.state, self.net, CFG["recipients"])
                   if x["vid"] not in self.notified[inc.event_id]]
            if not new:
                continue
            with tracer.trace("rules.update", service="hanlane-gateway", resource=inc.type) as span:
                t3 = now_ns()
                carrier: dict[str, str] = {}
                HTTPPropagator.inject(span.context, carrier)
                span.set_tag("event.update_of", str(inc.event_id))
                span.set_tag("event.recipients", len(new))
                doc = {
                    "_id": ObjectId(), "type": inc.type, "update_of": inc.event_id,
                    "status": "closed",  # a one-off notice; the incident lifecycle lives on the parent
                    "ts": ns_to_dt(t3), "sensor_ts": ns_to_dt(t0),
                    "loc": {"type": "Point", "coordinates": [rec["lon"], rec["lat"]]},
                    "edge": rec["edge"], "lane": rec["lane"], "zone": rec["zone"], "vid": inc.vid,
                    "severity": "update",
                    "recipients": [{"vid": x["vid"], "action": x["action"], "params": x["params"]} for x in new],
                    "trace": carrier, "timing": {"t0": t0, "t1": t1, "t3": t3},
                    "sim_time": self.state.sim_time,
                    "expireAt": ns_to_dt(t3) + timedelta(seconds=CFG["ttl"]["events_seconds"]),
                }
                self.events.insert(doc)
                if self.fast:
                    self.fast.send(doc)
            self.notified[inc.event_id].update(x["vid"] for x in new)
            until = time.time() + ADVISED_HOLD_S
            for x in new:
                self.state.advised_until[x["vid"]] = until
            statsd().increment("advisory.updates", tags=[f"event_type:{inc.type.lower()}"])
            self.counts["updates"] = self.counts.get("updates", 0) + 1

    def _probe(self, batch: dict, t0: int, t1: int) -> None:
        rec = next((r for r in batch["records"] if r["meta"]["vid"] == batch["probe"]["vid"]), batch["records"][0])
        with tracer.trace("rules.probe", service="hanlane-gateway", resource="PROBE") as span:
            t3 = now_ns()
            carrier: dict[str, str] = {}
            HTTPPropagator.inject(span.context, carrier)
            doc = {
                "_id": ObjectId(),  # set before handing the doc to two threads (insert + fast path)
                "type": "PROBE", "status": "closed", "ts": ns_to_dt(t3), "sensor_ts": ns_to_dt(t0),
                "loc": {"type": "Point", "coordinates": [rec["lon"], rec["lat"]]},
                "edge": rec["edge"], "lane": rec["lane"], "zone": rec["zone"], "vid": rec["meta"]["vid"],
                "severity": "none", "recipients": [], "trace": carrier,
                "timing": {"t0": t0, "t1": t1, "t3": t3}, "sim_time": self.state.sim_time,
                "expireAt": ns_to_dt(t3) + timedelta(seconds=CFG["ttl"]["probe_events_seconds"]),
            }
            self.events.insert(doc)
            if self.fast:
                self.fast.send(doc)
        statsd().distribution("detect.lag_ms", (t3 - t0) / 1e6, tags=["event_type:probe"])

    def _push_feed(self, item: dict) -> None:
        item["t"] = time.time()
        item["sim_time"] = self.state.sim_time
        self.feed.append(item)
        del self.feed[:-50]

    # ----------------------------------------------------------------- web
    def _build_snapshot(self) -> None:
        st = self.state
        advised = st.advised_until
        incident_vids = {i.vid for i in self.rules.open.values() if i.vid}
        vehicles = []
        for vid, v in st.vehicles.items():
            r = v.rec
            flag = 2 if vid in incident_vids else (1 if vid in advised else 0)
            vehicles.append([vid, r["meta"]["type"][0], r["lon"], r["lat"], round(r["speed"], 1),
                             round(r["heading"]), flag])
        open_events = []
        for inc in self.rules.open.values():
            ev = {"event_id": str(inc.event_id), "type": inc.type, "vid": inc.vid, "lane": inc.lane,
                  "opened_sim": inc.opened_sim}
            if inc.vid and inc.vid in st.vehicles:
                ev["lon"], ev["lat"] = st.vehicles[inc.vid].rec["lon"], st.vehicles[inc.vid].rec["lat"]
            else:
                lane_recs = [r for r in st.by_edge.get(inc.lane.rsplit("_", 1)[0], []) if r["lane"] == inc.lane]
                if lane_recs:
                    tail = min(lane_recs, key=lambda r: r["pos"])
                    ev["lon"], ev["lat"] = tail["lon"], tail["lat"]
            open_events.append(ev)
        self.snapshot = {
            "type": "snapshot", "sim_time": st.sim_time, "vehicles": vehicles, "open_events": open_events,
            "feed": self.feed[-15:], "stats": self.stats(),
        }
        self.snapshot_version += 1

    def stats(self) -> dict:
        sim = self.state.sim_stats or {}
        return {
            "vehicles": len(self.state.vehicles), "sim_time": self.state.sim_time,
            "write_mode": self.telemetry.mode, "pending_batches": self.telemetry.pending(),
            "dropped_batches": self.telemetry.dropped, "last_write_ms": self.telemetry.last_write_ms,
            "open_events": len(self.rules.open), "events": self.counts["events"],
            "batches": self.counts["batches"], "duplicates": self.counts["duplicates"],
            "advisory_updates": self.counts["updates"],
            "loop_p50": sim.get("loop_p50"), "loop_p95": sim.get("loop_p95"), "loop_n": sim.get("loop_n"),
            "overrun_pct": sim.get("overrun_pct"), "advisories_enabled": sim.get("advisories_enabled"),
            "scale": sim.get("scale"), "incident": sim.get("incident"),
        }

    def sim_control(self, payload: dict) -> dict:
        try:
            resp = self.sim.post("/control", json=payload)
            return resp.json()
        except httpx.HTTPError as e:
            raise HTTPException(502, f"sim-bridge unreachable: {e}") from e


gw: Gateway | None = None


def rollup_loop() -> None:
    """Every minute: $merge the minute that just ended into rollups_1m (one aggregation)."""
    db = get_client("hanlane-gateway")[S.mongodb_db]
    while True:
        time.sleep(60)
        if not gw or not gw.state.vehicles:
            continue
        try:
            with tracer.trace("rollup.merge", service="hanlane-gateway", resource="rollups_1m"):
                rollup.run(db, minutes=1)   # the minute that just ended, written once
        except Exception as e:  # noqa: BLE001 - rollups are best effort
            log.warning("rollup failed", extra={"error": str(e)})


@asynccontextmanager
async def lifespan(app: FastAPI):
    global gw
    gw = Gateway()
    threading.Thread(target=rollup_loop, name="rollup", daemon=True).start()
    log.info("gateway ready", extra={"write_mode": gw.telemetry.mode, "edges": len(gw.net.getEdges())})
    yield


app = FastAPI(title="HanLane ingest-gateway", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict:
    get_client("hanlane-gateway").admin.command("ping")
    return {"status": "ok", "mongo": "ok"}


@app.post("/ingest")
def ingest(batch: dict) -> dict:
    return gw.ingest(batch)


@app.get("/api/stats")
def api_stats() -> dict:
    return gw.stats()


@app.get("/api/config")
def api_config() -> dict:
    style = S.map_style_url
    if style and S.map_api_key and "{key}" in style:
        style = style.replace("{key}", S.map_api_key)
    elif style and S.map_api_key and "key=" not in style:
        style += ("&" if "?" in style else "?") + f"key={S.map_api_key}"
    return {"map_style_url": style, "center": [108.2275, 16.0613], "zoom": 15}


class IncidentReq(BaseModel):
    edge: str | None = None


class ToggleReq(BaseModel):
    enabled: bool


class ScaleReq(BaseModel):
    scale: float


class ModeReq(BaseModel):
    mode: str


@app.post("/api/control/incident")
def control_incident(req: IncidentReq) -> dict:
    return gw.sim_control({"cmd": "incident", "edge": req.edge})


@app.post("/api/control/clear")
def control_clear() -> dict:
    return gw.sim_control({"cmd": "clear"})


@app.post("/api/control/advisories")
def control_advisories(req: ToggleReq) -> dict:
    return gw.sim_control({"cmd": "advisories", "enabled": req.enabled})


@app.post("/api/control/scale")
def control_scale(req: ScaleReq) -> dict:
    return gw.sim_control({"cmd": "scale", "scale": req.scale})


@app.post("/api/control/reset")
def control_reset() -> dict:
    """Forget live state, open incidents and seen batches (used between A/B runs)."""
    with gw.lock:
        rules = CFG["rules"]
        gw.state = LiveState(rules["stalled_speed"], rules["close_speed"])
        gw.rules.open.clear()
        gw.notified.clear()
        gw.feed.clear()
        gw.snapshot = {}
    log.info("gateway state reset")
    return {"reset": True}


@app.post("/api/control/write-mode")
def control_write_mode(req: ModeReq) -> dict:
    if req.mode not in ("batch", "single"):
        raise HTTPException(400, "mode must be batch or single")
    gw.telemetry.mode = req.mode
    log.info("write mode changed", extra={"write_mode": req.mode})
    return {"write_mode": req.mode}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    sent = -1
    try:
        while True:
            if gw.snapshot_version != sent and gw.snapshot:
                sent = gw.snapshot_version
                await websocket.send_text(json.dumps(gw.snapshot, default=str))
            await asyncio.sleep(0.2)
    except (WebSocketDisconnect, RuntimeError):
        return
