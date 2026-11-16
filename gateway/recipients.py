"""Pick the vehicles to warn: upstream of the hazard within the radius, from RAM and the road graph.

Vehicles closer than min_headway_s are left out: that is collision avoidance, not V2N.
"""
from __future__ import annotations

from common.roadnet import upstream_distances
from gateway.rules import Detection
from gateway.state import LiveState


def select(det: Detection, st: LiveState, net, cfg: dict, max_recipients: int = 30) -> list[dict]:
    r = det.rec
    hazard_edge, hazard_pos = r["edge"], r["pos"]
    if hazard_edge.startswith(":"):
        return []
    subject = r["meta"]["vid"]
    up = upstream_distances(net, hazard_edge, hazard_pos, cfg["radius_m"])
    lanes_on_edge = net.getEdge(hazard_edge).getLaneNumber() if net.hasEdge(hazard_edge) else 1
    seconds = cfg["slowdown_seconds"].get(det.type, 4.0)

    cands = []
    for edge, base in up.items():
        for v in st.by_edge.get(edge, []):
            vid = v["meta"]["vid"]
            if vid == subject:
                continue
            dist = base - v["pos"]
            if not (0 < dist <= cfg["radius_m"]):
                continue
            speed = v["speed"]
            if speed > 0.5 and dist / speed < cfg["min_headway_s"]:
                continue  # tailgating: out of scope
            cands.append((dist, v))
    cands.sort(key=lambda c: c[0])

    out: list[dict] = []
    for dist, v in cands[:max_recipients]:
        vid, speed = v["meta"]["vid"], v["speed"]
        if speed >= 1.0:
            target = round(max(speed * cfg["slowdown_factor"], min(cfg["slowdown_min_speed"], speed)), 1)
            out.append({"vid": vid, "action": "slowDown", "dist": round(dist, 1),
                        "params": {"speed": target, "duration": seconds}})
        if det.type == "STALLED" and v["edge"] == hazard_edge and v["laneIndex"] == r["laneIndex"]:
            target_lane = _free_adjacent_lane(v, st, lanes_on_edge, cfg["adjacent_lane_clear_m"])
            if target_lane is not None:
                out.append({"vid": vid, "action": "changeLane", "dist": round(dist, 1),
                            "params": {"laneIndex": target_lane, "duration": cfg["change_lane_seconds"]}})
    return out


def _free_adjacent_lane(v: dict, st: LiveState, n_lanes: int, clear_m: float) -> int | None:
    for target in (v["laneIndex"] + 1, v["laneIndex"] - 1):
        if not (0 <= target < n_lanes):
            continue
        blocked = any(
            o["laneIndex"] == target and abs(o["pos"] - v["pos"]) < clear_m
            for o in st.by_edge.get(v["edge"], [])
        )
        if not blocked:
            return target
    return None
