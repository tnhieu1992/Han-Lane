"""Detection rules on the in-RAM state. Thresholds come from config [rules].

Incidents have a lifecycle: opened on detection, closed when the vehicle moves again
(or the lane recovers); an open incident never re-fires.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bson import ObjectId

from gateway.state import LiveState

RED_STATES = set("rRyYu")


@dataclass
class Detection:
    type: str
    key: tuple
    rec: dict                      # record locating the hazard (subject vehicle or queue tail)
    severity: str
    details: dict = field(default_factory=dict)


@dataclass
class OpenIncident:
    event_id: ObjectId
    type: str
    key: tuple
    vid: str | None
    lane: str
    opened_sim: float


class RuleEngine:
    def __init__(self, cfg: dict, lane_speed_limit, lane_length):
        self.c = cfg
        self.lane_limit = lane_speed_limit
        self.lane_length = lane_length
        self.open: dict[tuple, OpenIncident] = {}

    # ---------------------------------------------------------------- detection
    def evaluate(self, st: LiveState) -> list[Detection]:
        out: list[Detection] = []
        c = self.c
        for vid, v in st.vehicles.items():
            r = v.rec
            vtype = r["meta"].get("type", "car")
            hb_key = ("HARD_BRAKE", vid)
            if hb_key not in self.open and r["accel"] < c["hard_brake_accel"].get(vtype, -6.0):
                out.append(Detection("HARD_BRAKE", hb_key, r, "medium",
                                     {"accel": r["accel"], "speed": r["speed"]}))
            st_key = ("STALLED", vid)
            if st_key not in self.open and self._stalled(st, v):
                out.append(Detection("STALLED", st_key, r, "high",
                                     {"hazard": r["signals"]["hazard"], "speed": r["speed"],
                                      "stopped_s": round(st.sim_time - (v.stopped_since or st.sim_time), 1)}))
        out.extend(self._lane_jams(st))
        return out

    def _stalled(self, st: LiveState, v) -> bool:
        r, c = v.rec, self.c
        if r["speed"] >= c["stalled_speed"]:
            return False
        if r["signals"]["hazard"]:
            return True
        if v.stopped_since is None or st.sim_time - v.stopped_since < c["stalled_seconds"]:
            return False
        # Head of the queue: no stopped vehicle ahead within stalled_leader_clear_m.
        if r.get("leader") and r.get("leaderGap") is not None and r["leaderGap"] <= c["stalled_leader_clear_m"]:
            lead = st.vehicles.get(r["leader"])
            if lead is None or lead.rec["speed"] < c["stalled_speed"]:
                return False
        # Not waiting at a red light.
        tls = r.get("nextTls")
        if tls and tls["state"] in RED_STATES and tls["dist"] <= c["stalled_tls_dist_m"]:
            return False
        if r["lane"].startswith(":"):
            return False  # inside a junction: queueing, not a stall
        length = self.lane_length(r["lane"])
        if length and length - r["pos"] < c["stalled_junction_clear_m"]:
            return False  # waiting to yield at the end of the lane (unsignalised junction)
        return True

    def _lane_jams(self, st: LiveState) -> list[Detection]:
        c = self.c
        out = []
        need = c["lane_jam_seconds"]
        for lane, w in st.lanes.items():
            key = ("LANE_JAM", lane)
            if key in self.open or w.last_seen != st.sim_time:
                continue
            samples = st.lane_window(lane, need)
            if len(samples) < 0.9 * need:
                continue
            total = sum(n for _, _, n in samples)
            mean = sum(m * n for _, m, n in samples) / total
            limit = self.lane_limit(lane)
            if not limit or mean >= c["lane_jam_ratio"] * limit:
                continue
            edge = lane.rsplit("_", 1)[0]
            on_lane = [r for r in st.by_edge.get(edge, []) if r["lane"] == lane]
            if len(on_lane) < c["lane_jam_min_vehicles"]:
                continue
            head = max(on_lane, key=lambda r: r["pos"])
            tls = head.get("nextTls")
            if tls and tls["state"] in RED_STATES and tls["dist"] <= c["stalled_tls_dist_m"]:
                continue  # queue at a red light
            tail = min(on_lane, key=lambda r: r["pos"])
            out.append(Detection("LANE_JAM", key, tail, "medium",
                                 {"mean_speed": round(mean, 2), "limit": round(limit, 2),
                                  "vehicles": len(on_lane), "head_vid": head["meta"]["vid"]}))
        return out

    # ---------------------------------------------------------------- lifecycle
    def opened(self, det: Detection, event_id: ObjectId, sim_time: float) -> None:
        vid = det.rec["meta"]["vid"] if det.type != "LANE_JAM" else None
        self.open[det.key] = OpenIncident(event_id, det.type, det.key, vid, det.rec["lane"], sim_time)

    def closures(self, st: LiveState) -> list[tuple[OpenIncident, str]]:
        c = self.c
        out = []
        for key, inc in list(self.open.items()):
            reason = None
            if inc.vid is not None:
                v = st.vehicles.get(inc.vid)
                if v is None:
                    reason = "vehicle_left"
                elif inc.type == "HARD_BRAKE":
                    # Momentary: closes once the vehicle has stopped braking hard for close_seconds.
                    if st.sim_time - inc.opened_sim >= c["close_seconds"] and v.rec["accel"] > -2.0:
                        reason = "braking_ended"
                elif (v.moving_since is not None and v.moving_since > inc.opened_sim
                      and st.sim_time - v.moving_since >= c["close_seconds"]):
                    reason = "moving_again"
            else:
                samples = st.lane_window(inc.lane, 10)
                limit = self.lane_limit(inc.lane)
                if not samples:
                    reason = "lane_empty"
                elif limit and sum(m for _, m, _ in samples) / len(samples) > 0.5 * limit:
                    reason = "lane_recovered"
            if reason:
                del self.open[key]
                out.append((inc, reason))
        return out

    def summary(self) -> list[dict[str, Any]]:
        return [{"event_id": str(i.event_id), "type": i.type, "vid": i.vid, "lane": i.lane,
                 "opened_sim": i.opened_sim} for i in self.open.values()]
