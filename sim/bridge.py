"""sim-bridge: runs SUMO through TraCI in real time and streams telemetry to the gateway.

- 0.2 s steps paced to wall time; one telemetry batch per simulated second.
- Vehicle state comes from TraCI subscriptions (no per-vehicle getter calls).
- Advisories arrive over HTTP into a queue and are applied at the start of the next step.
- t0 (read) and t5 (applied) are both taken here, so loop latency has no clock skew.

    python -m sim.bridge [--gui] [--scale 0.4] [--seed 42]
    A/B: --fast-until 285 --incident-at 300 --end 420 --advisories off --ab-out out.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import queue
import random
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx
import sumolib
import traci
import traci.constants as tc
from ddtrace.trace import tracer

from common.logging import setup
from common.metrics import statsd
from common.roadnet import upstream_distances
from common.settings import config, path, settings
from common.timeutil import now_ns
from sim.control_server import Command, ControlServer

log = setup("hanlane-sim")

SIGNAL_HAZARD = 1 << 2   # VEH_SIGNAL_BLINKER_EMERGENCY
SIGNAL_BRAKE = 1 << 3    # VEH_SIGNAL_BRAKELIGHT
TYPE_PREFIX = {"motorbike": "moto", "car": "car", "bus": "bus"}
TYPE_COLOR = {"motorbike": (255, 204, 0, 255), "car": (77, 153, 255, 255), "bus": (51, 204, 77, 255)}
ADVISED_COLOR = (255, 120, 0, 255)
INCIDENT_COLOR = (230, 30, 30, 255)
ADVISED_HOLD_S = 20.0

VEH_VARS = [
    tc.VAR_POSITION, tc.VAR_SPEED, tc.VAR_ACCELERATION, tc.VAR_ANGLE, tc.VAR_ROAD_ID, tc.VAR_LANE_ID,
    tc.VAR_LANE_INDEX, tc.VAR_LANEPOSITION, tc.VAR_LANEPOSITION_LAT, tc.VAR_SIGNALS, tc.VAR_TYPE,
    tc.VAR_LEADER, tc.VAR_NEXT_TLS,
]


def pct(values, q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


class SimBridge:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cfg = config()
        self.s = settings()
        sim = self.cfg["sim"]
        self.step = float(sim["step_length"])
        self.net_file = path(sim["net_file"])
        self.net = sumolib.net.readNet(str(self.net_file), withInternal=False)
        self.zones: dict[str, str] = json.loads(path(sim["zones_file"]).read_text())
        meta = json.loads(path(sim["meta_file"]).read_text())
        self.default_edge = self.cfg["incident"]["default_edge"] or meta["incident_edge"]
        self.rng = random.Random(args.seed)
        self.gps_noise = float(sim["gps_noise_m"])
        self.salt = f"{args.seed}-{time.time_ns()}" if not args.stable_ids else str(args.seed)
        self.run_id = hashlib.sha1(f"{self.salt}-{time.time_ns()}".encode()).hexdigest()[:8]  # unique per run

        self.vid_of: dict[str, str] = {}
        self.sid_of: dict[str, str] = {}
        self.type_of: dict[str, str] = {}
        self.last_zone: dict[str, str] = {}
        self.advised: dict[str, float] = {}
        self.incident: dict[str, Any] | None = None
        self.advisories_enabled = args.advisories == "on"
        self.scale = args.scale if args.scale is not None else float(sim["scale"])
        self.sim_time = 0.0
        self.epoch_ms = (int(time.time()) // 1) * 1000  # sim second 0 anchored to wall clock
        self.steps = 0
        self.overruns = 0
        self.loop_ms: deque[float] = deque(maxlen=120)
        self.seg_ms: dict[str, deque] = {}
        self.path_counts: dict[str, int] = {}
        self.applied_events = 0
        self.running = True

        self.commands: queue.Queue[Command] = queue.Queue()
        self.send_q: queue.Queue[dict] = queue.Queue(maxsize=10)
        self.control = ControlServer(int(sim["control_port"]), self.commands, self.status)
        self.ab: dict[str, Any] | None = {"followers": {}, "collisions": 0, "warned": set()} if args.ab_out else None

    # ------------------------------------------------------------------ lifecycle
    def start_sumo(self) -> None:
        sim = self.cfg["sim"]
        binary = "sumo-gui" if self.args.gui else "sumo"
        cmd = [
            str(Path(sys.executable).parent / binary),
            "-n", str(self.net_file),
            "-r", f"{path(sim['route_file'])},{path(sim['flows_file'])}",
            "-a", str(path(sim["vtypes_file"])),
            "--step-length", str(self.step),
            "--lateral-resolution", str(sim["lateral_resolution"]),
            "--seed", str(self.args.seed),
            "--scale", str(self.scale),
            "--time-to-teleport", str(self.args.time_to_teleport),
            "--collision.action", "warn",
            "--no-step-log", "--no-warnings",
        ]
        if self.args.gui:
            cmd += ["--start", "--quit-on-end", "--delay", "0"]
        traci.start(cmd)
        traci.simulation.subscribe([tc.VAR_DEPARTED_VEHICLES_IDS, tc.VAR_ARRIVED_VEHICLES_IDS])
        log.info("sumo started", extra={"binary": binary, "scale": self.scale, "seed": self.args.seed,
                                         "run_id": self.run_id, "incident_edge": self.default_edge})

    def run(self) -> None:
        self.start_sumo()
        self.control.start()
        sender = threading.Thread(target=self._sender_loop, name="batch-sender", daemon=True)
        sender.start()
        t_next = time.perf_counter()
        try:
            while self.running:
                self._drain_commands()
                traci.simulationStep()
                self.steps += 1
                self.sim_time = traci.simulation.getTime()
                self._track_vehicles()
                self._incident_tick()
                if self.ab is not None:
                    self._ab_tick()
                realtime = self.sim_time >= self.args.fast_until
                whole_second = abs(self.sim_time - round(self.sim_time)) < 1e-6
                if whole_second:
                    self._per_second(send=realtime and not self.args.no_send)
                if self.args.incident_at is not None and self.incident is None and \
                        abs(self.sim_time - self.args.incident_at) < 1e-6:
                    self._cmd_incident({"edge": self.args.incident_edge or self.default_edge})
                if self.args.end and self.sim_time >= self.args.end:
                    break
                if traci.simulation.getMinExpectedNumber() == 0:
                    log.info("no more vehicles expected, stopping")
                    break
                if realtime:
                    t_next += self.step
                    delay = t_next - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        self.overruns += 1
                        if delay < -1.0:
                            t_next = time.perf_counter()
                else:
                    t_next = time.perf_counter()
        finally:
            self.running = False
            self._write_ab()
            try:
                traci.close()
            except Exception:  # noqa: BLE001
                pass
            self.control.stop()
            log.info("sim stopped", extra=self.status())

    # ------------------------------------------------------------------ vehicles
    def _track_vehicles(self) -> None:
        res = traci.simulation.getSubscriptionResults()
        for sid in res.get(tc.VAR_DEPARTED_VEHICLES_IDS, ()):
            traci.vehicle.subscribe(sid, VEH_VARS, parameters={tc.VAR_LEADER: ("d", 100.0)})
            vtype = traci.vehicle.getTypeID(sid)
            self.type_of[sid] = vtype
            digest = hashlib.sha1(f"{self.salt}:{sid}".encode()).hexdigest()[:6]
            vid = f"{TYPE_PREFIX.get(vtype, 'veh')}-{digest}"
            self.vid_of[sid] = vid
            self.sid_of[vid] = sid
        for sid in res.get(tc.VAR_ARRIVED_VEHICLES_IDS, ()):
            vid = self.vid_of.pop(sid, None)
            if vid:
                self.sid_of.pop(vid, None)
            self.type_of.pop(sid, None)
            self.last_zone.pop(sid, None)
            self.advised.pop(sid, None)

    def _per_second(self, send: bool) -> None:
        results = traci.vehicle.getAllSubscriptionResults()
        t0 = now_ns()
        statsd().gauge("vehicles.active", len(results), tags=["service:hanlane-sim"])
        # Restore colours of vehicles whose advisory hold expired.
        for sid, until in list(self.advised.items()):
            if self.sim_time >= until:
                self.advised.pop(sid, None)
                if sid in results and not (self.incident and self.incident.get("sid") == sid):
                    traci.vehicle.setColor(sid, TYPE_COLOR.get(self.type_of.get(sid, ""), (200, 200, 200, 255)))
        if not send or not results:
            return
        ts_ms = self.epoch_ms + int(round(self.sim_time * 1000))
        records = [self._record(sid, r) for sid, r in results.items() if sid in self.vid_of]
        batch = {
            "batch_id": f"{self.run_id}-{int(round(self.sim_time))}",
            "sim_time": self.sim_time,
            "ts": ts_ms,
            "t0": t0,
            "probe": {"vid": records[0]["meta"]["vid"]} if records else None,
            "records": records,
            "stats": self._stats(),
        }
        try:
            self.send_q.put_nowait(batch)
        except queue.Full:
            try:
                self.send_q.get_nowait()  # drop the oldest, keep the loop real time
            except queue.Empty:
                pass
            self.send_q.put_nowait(batch)

    def _record(self, sid: str, r: dict) -> dict:
        x, y = r[tc.VAR_POSITION]
        if self.gps_noise > 0:
            x += self.rng.gauss(0, self.gps_noise)
            y += self.rng.gauss(0, self.gps_noise)
        lon, lat = self.net.convertXY2LonLat(x, y)
        edge = r[tc.VAR_ROAD_ID]
        zone = self.zones.get(edge) or self.last_zone.get(sid, "other")
        self.last_zone[sid] = zone
        sig = int(r[tc.VAR_SIGNALS])
        leader = r.get(tc.VAR_LEADER)
        leader_vid, leader_gap = None, None
        if leader and leader[0]:
            leader_vid, leader_gap = self.vid_of.get(leader[0]), round(float(leader[1]), 1)
        tls = r.get(tc.VAR_NEXT_TLS) or ()
        next_tls = {"state": tls[0][3], "dist": round(float(tls[0][2]), 1)} if tls else None
        return {
            "meta": {"vid": self.vid_of[sid], "type": self.type_of.get(sid, "unknown")},
            "lon": round(lon, 7), "lat": round(lat, 7),
            "zone": zone, "edge": edge, "lane": r[tc.VAR_LANE_ID], "laneIndex": r[tc.VAR_LANE_INDEX],
            "pos": round(float(r[tc.VAR_LANEPOSITION]), 1),
            "speed": round(float(r[tc.VAR_SPEED]), 2),
            "accel": round(float(r[tc.VAR_ACCELERATION]), 2),
            "heading": round(float(r[tc.VAR_ANGLE]), 1),
            "latOffset": round(float(r[tc.VAR_LANEPOSITION_LAT]), 2),
            "leader": leader_vid, "leaderGap": leader_gap,
            "signals": {"brake": bool(sig & SIGNAL_BRAKE), "hazard": bool(sig & SIGNAL_HAZARD)},
            "nextTls": next_tls,
        }

    def _stats(self) -> dict:
        loop = list(self.loop_ms)
        return {
            "sim_time": self.sim_time,
            "loop_p50": pct(loop, 0.5), "loop_p95": pct(loop, 0.95), "loop_n": len(loop),
            "overrun_pct": round(100.0 * self.overruns / max(1, self.steps), 2),
            "advisories_enabled": self.advisories_enabled,
            "scale": self.scale,
            "incident": {k: self.incident.get(k) for k in ("vid", "edge", "phase")} if self.incident else None,
        }

    def status(self) -> dict:
        segments = {k: {"p50": pct([x for x in v if x is not None], 0.5),
                        "p95": pct([x for x in v if x is not None], 0.95)} for k, v in self.seg_ms.items()}
        return {"status": "ok", "run_id": self.run_id, "vehicles": len(self.vid_of), **self._stats(),
                "segments_ms": segments, "dispatch_paths": self.path_counts}

    # ------------------------------------------------------------------ sending
    def _sender_loop(self) -> None:
        client = httpx.Client(base_url=self.s.gateway_url, timeout=float(self.cfg["sim"]["batch_timeout_s"]))
        while self.running or not self.send_q.empty():
            try:
                batch = self.send_q.get(timeout=0.5)
            except queue.Empty:
                continue
            for attempt in range(3):
                try:
                    with tracer.trace("sim.batch.send", service="hanlane-sim", resource="POST /ingest") as span:
                        span.set_tag("batch.size", len(batch["records"]))
                        span.set_tag("batch.attempt", attempt)
                        resp = client.post("/ingest", json=batch)
                    if resp.status_code < 500:
                        break
                except httpx.HTTPError as e:
                    if attempt == 2:
                        log.warning("batch send failed", extra={"batch_id": batch["batch_id"], "error": str(e)})
                    time.sleep(0.2)

    # ------------------------------------------------------------------ commands
    def _drain_commands(self) -> None:
        while True:
            try:
                cmd = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                if cmd.kind == "advisory":
                    self._apply_advisory(cmd)
                else:
                    cmd.result = self._control(cmd.payload)
            except traci.TraCIException as e:
                cmd.result = {"error": str(e)}
                log.warning("command failed", extra={"kind": cmd.kind, "error": str(e)})
            finally:
                cmd.done.set()

    def _control(self, p: dict) -> dict:
        op = p.get("cmd")
        if op == "incident":
            return self._cmd_incident(p)
        if op == "clear":
            return self._cmd_clear()
        if op == "advisories":
            self.advisories_enabled = bool(p.get("enabled", True))
            log.info("advisories toggled", extra={"enabled": self.advisories_enabled})
            return {"advisories_enabled": self.advisories_enabled}
        if op == "scale":
            self.scale = float(p["scale"])
            traci.simulation.setScale(self.scale)
            log.info("demand scale changed", extra={"scale": self.scale})
            return {"scale": self.scale}
        return {"error": f"unknown cmd {op!r}"}

    def _apply_advisory(self, cmd: Command) -> None:
        p = cmd.payload
        etype = p.get("event_type", "UNKNOWN")
        span = tracer.start_span("sim.advisory.apply", child_of=cmd.trace_ctx, service="hanlane-sim",
                                 resource=etype, activate=True)
        applied, missing, blocked = 0, 0, 0
        try:
            if etype != "PROBE" and self.advisories_enabled:
                for item in p.get("recipients", []):
                    sid = self.sid_of.get(item["vid"])
                    if sid is None:
                        missing += 1
                        continue
                    params = item.get("params", {})
                    if item["action"] == "slowDown":
                        traci.vehicle.slowDown(sid, float(params["speed"]), float(params["duration"]))
                    elif item["action"] == "changeLane":
                        target = int(params["laneIndex"])
                        current = traci.vehicle.getLaneIndex(sid)
                        direction = 1 if target > current else -1
                        if target != current and traci.vehicle.couldChangeLane(sid, direction):
                            traci.vehicle.changeLane(sid, target, float(params.get("duration", 5.0)))
                        else:
                            blocked += 1
                            continue
                    traci.vehicle.setColor(sid, ADVISED_COLOR)
                    self.advised[sid] = self.sim_time + ADVISED_HOLD_S
                    if self.ab is not None:
                        self.ab["warned"].add(sid)
                    applied += 1
            t5 = now_ns()
            self._record_latency(p, t5, applied, missing, blocked)
            span.set_tag("advisory.applied", applied)
            span.set_tag("advisory.missing", missing)
        finally:
            span.finish()

    def _record_latency(self, p: dict, t5: int, applied: int, missing: int, blocked: int) -> None:
        etype = p.get("event_type", "UNKNOWN")
        tag = f"event_type:{etype.lower()}"
        path = p.get("path", "changestream")
        t0, t1, t3, t4, t_send = (p.get(k) for k in ("t0", "t1", "t3", "t4", "t_send"))
        if t0 is None:
            return
        loop = (t5 - t0) / 1e6
        self.loop_ms.append(loop)
        sd = statsd()
        sd.distribution("loop.latency_ms", loop, tags=[tag, "service:hanlane-sim"])
        segments = {"t0_t1": (t0, t1), "t1_t3": (t1, t3), "t3_t4": (t3, t4), "t4_send": (t4, t_send),
                    "send_t5": (t_send, t5)}
        seg_ms = {}
        for name, (a, b) in segments.items():
            if a is not None and b is not None:
                seg_ms[name] = round((b - a) / 1e6, 1)
                self.seg_ms.setdefault(name, deque(maxlen=120)).append(seg_ms[name])
                sd.distribution("segment_ms", seg_ms[name],
                                tags=[f"segment:{name}", tag, f"path:{path}", "service:hanlane-sim"])
        self.seg_ms.setdefault(f"t3_t4.{path}", deque(maxlen=120)).append(seg_ms.get("t3_t4"))
        self.path_counts[path] = self.path_counts.get(path, 0) + 1
        if etype != "PROBE":
            if applied:
                self.applied_events += 1
            log.info("advisory applied", extra={
                "event_id": p.get("event_id"), "event_type": etype, "loop_latency_ms": round(loop, 1),
                "segments_ms": seg_ms, "path": path, "recipients": len(p.get("recipients", [])), "applied": applied,
                "missing": missing, "lane_change_blocked": blocked, "advisories_enabled": self.advisories_enabled,
                "latitude": p.get("lat"), "longitude": p.get("lon"),
            })

    # ------------------------------------------------------------------ incident
    def _cmd_incident(self, p: dict) -> dict:
        if self.incident:
            return {"error": "incident already active", "incident": self._stats()["incident"]}
        edge = p.get("edge") or self.default_edge
        self.incident = {"edge": edge, "phase": "pending", "requested": self.sim_time, "sid": None, "vid": None}
        self._incident_tick()
        log.info("incident requested", extra={"edge": edge, "phase": self.incident["phase"]})
        return {"incident": self._stats()["incident"]}

    def _pick_incident_vehicle(self, edge: str) -> str | None:
        results = traci.vehicle.getAllSubscriptionResults()
        length = self.net.getEdge(edge).getLength() if self.net.hasEdge(edge) else 1e9
        on_edge = [(sid, r) for sid, r in results.items() if r[tc.VAR_ROAD_ID] == edge]
        if not on_edge:
            return None

        def followers(r: dict) -> int:
            pos = r[tc.VAR_LANEPOSITION]
            return sum(1 for _, o in on_edge if 0 < pos - o[tc.VAR_LANEPOSITION] <= 150)

        for strict in (True, False):
            cands = [
                (sid, r) for sid, r in on_edge
                if not strict or (r[tc.VAR_LANE_INDEX] == 0 and 0.3 * length <= r[tc.VAR_LANEPOSITION] <= 0.85 * length
                                  and r[tc.VAR_SPEED] > 6.0 and followers(r) >= 1)
            ]
            if cands:
                return max(cands, key=lambda c: (followers(c[1]), c[1][tc.VAR_LANEPOSITION]))[0]
        return None

    def _incident_tick(self) -> None:
        inc = self.incident
        if not inc:
            return
        sid = inc.get("sid")
        if sid and sid not in self.vid_of:
            log.warning("incident vehicle left the network", extra={"vid": inc.get("vid")})
            self.incident = None
            return
        if inc["phase"] == "pending":
            if self.sim_time - inc["requested"] > 60:
                log.warning("no vehicle found for incident", extra={"edge": inc["edge"]})
                self.incident = None
                return
            sid = self._pick_incident_vehicle(inc["edge"])
            if sid is None:
                return
            speed = traci.vehicle.getSpeed(sid)
            decel = float(self.cfg["incident"]["decel"])
            traci.vehicle.setSpeedMode(sid, 27)          # ignore max decel, keep safe-speed checks
            traci.vehicle.setLaneChangeMode(sid, 0)
            traci.vehicle.slowDown(sid, 0.0, max(speed / decel, 0.4))
            traci.vehicle.setColor(sid, INCIDENT_COLOR)
            inc.update(sid=sid, vid=self.vid_of[sid], phase="braking", start_speed=round(speed, 1))
            log.info("incident vehicle braking", extra={"vid": inc["vid"], "edge": inc["edge"], "speed": speed})
        elif inc["phase"] == "braking":
            if traci.vehicle.getSpeed(sid) < 0.1:
                traci.vehicle.setSpeed(sid, 0.0)
                traci.vehicle.setSignals(sid, SIGNAL_HAZARD | SIGNAL_BRAKE)
                inc["phase"] = "stopped"
                inc["stopped_at"] = self.sim_time
                inc["pos"] = traci.vehicle.getLanePosition(sid)
                inc["lane"] = traci.vehicle.getLaneID(sid)
                log.info("incident vehicle stopped, hazard on", extra={"vid": inc["vid"], "edge": inc["edge"]})

    def _cmd_clear(self) -> dict:
        inc = self.incident
        if not inc:
            return {"cleared": False, "reason": "no incident"}
        sid = inc.get("sid")
        if sid and sid in self.vid_of:
            traci.vehicle.setSpeed(sid, -1)
            traci.vehicle.setSpeedMode(sid, 31)
            traci.vehicle.setLaneChangeMode(sid, 1621)
            traci.vehicle.setSignals(sid, -1)
            traci.vehicle.setColor(sid, TYPE_COLOR.get(self.type_of.get(sid, ""), (200, 200, 200, 255)))
        self.incident = None
        log.info("incident cleared", extra={"vid": inc.get("vid"), "edge": inc.get("edge")})
        return {"cleared": True, "vid": inc.get("vid")}

    # ------------------------------------------------------------------ A/B metrics
    def _ab_tick(self) -> None:
        inc = self.incident
        if not inc or inc.get("phase") not in ("braking", "stopped"):
            return
        sid_inc = inc["sid"]
        results = traci.vehicle.getAllSubscriptionResults()
        r_inc = results.get(sid_inc)
        if r_inc is None:
            return
        edge, pos = r_inc[tc.VAR_ROAD_ID], r_inc[tc.VAR_LANEPOSITION]
        upstream = upstream_distances(self.net, edge, pos, 150.0)
        for sid, r in results.items():
            if sid == sid_inc:
                continue
            e = r[tc.VAR_ROAD_ID]
            if e not in upstream:
                continue
            dist = upstream[e] - r[tc.VAR_LANEPOSITION]
            if not (0 < dist <= 150):
                continue
            f = self.ab["followers"].setdefault(sid, {"type": self.type_of.get(sid), "min_accel": 0.0,
                                                     "min_gap": None, "min_headway": None})
            f["min_accel"] = min(f["min_accel"], float(r[tc.VAR_ACCELERATION]))
            leader = r.get(tc.VAR_LEADER)
            if leader and leader[0] and float(leader[1]) >= 0:   # sublane: negative = leader partly alongside
                gap = float(leader[1])
                f["min_gap"] = gap if f["min_gap"] is None else min(f["min_gap"], gap)
                v = float(r[tc.VAR_SPEED])
                if v > 0.5:
                    hw = gap / v
                    f["min_headway"] = hw if f["min_headway"] is None else min(f["min_headway"], hw)
        self.ab["collisions"] += traci.simulation.getCollidingVehiclesNumber()

    def _write_ab(self) -> None:
        if self.ab is None:
            return
        followers = self.ab["followers"].values()
        hard = [f for f in followers if f["min_accel"] < -4.5]   # beyond normal max decel = emergency
        harsh = [f for f in followers if f["min_accel"] < -3.0]
        gaps = [f["min_gap"] for f in followers if f["min_gap"] is not None]
        hws = [f["min_headway"] for f in followers if f["min_headway"] is not None]
        out = {
            "advisories": "on" if self.advisories_enabled else "off",
            "seed": self.args.seed, "scale": self.scale, "incident_at": self.args.incident_at,
            "incident": {k: v for k, v in (self.incident or {}).items() if k != "sid"},
            "followers": len(self.ab["followers"]),
            "emergency_brakers": len(hard), "harsh_brakers": len(harsh),
            "min_gap_m": round(min(gaps), 2) if gaps else None,
            "min_headway_s": round(min(hws), 2) if hws else None,
            "collisions": self.ab["collisions"],
            "advisory_events_applied": self.applied_events,
            "warned_followers": len([sid for sid in self.ab["followers"] if sid in self.ab["warned"]]),
            "loop_p95_ms": pct(list(self.loop_ms), 0.95),
        }
        Path(self.args.ab_out).write_text(json.dumps(out, indent=2))
        log.info("ab metrics written", extra=out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gui", action="store_true")
    ap.add_argument("--scale", type=float)
    ap.add_argument("--seed", type=int, default=config()["sim"]["seed"])
    ap.add_argument("--end", type=float, default=0, help="stop at this sim time (0 = run until routes end)")
    ap.add_argument("--time-to-teleport", type=int, default=-1)
    ap.add_argument("--fast-until", type=float, default=0, help="run unpaced and silent until this sim time")
    ap.add_argument("--no-send", action="store_true", help="do not send telemetry")
    ap.add_argument("--advisories", choices=["on", "off"], default="on")
    ap.add_argument("--incident-at", type=float, help="trigger an incident at this sim time")
    ap.add_argument("--incident-edge")
    ap.add_argument("--ab-out", help="write follower safety metrics to this JSON file")
    ap.add_argument("--stable-ids", action="store_true", help="same pseudonyms across runs (A/B)")
    args = ap.parse_args()

    bridge = SimBridge(args)

    def stop(*_):
        bridge.running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    bridge.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
