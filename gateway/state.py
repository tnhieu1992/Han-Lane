"""Live state in RAM: latest record per vehicle, per-lane sliding windows, batch dedup."""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Vehicle:
    rec: dict
    stopped_since: float | None = None   # sim time speed dropped below the stalled threshold
    moving_since: float | None = None    # sim time speed rose above the close threshold
    last_seen: float = 0.0


@dataclass
class LaneWindow:
    samples: deque = field(default_factory=lambda: deque(maxlen=120))  # (sim_sec, mean_speed, count)
    last_seen: float = 0.0


class LiveState:
    def __init__(self, stalled_speed: float, close_speed: float):
        self.stalled_speed = stalled_speed
        self.close_speed = close_speed
        self.vehicles: dict[str, Vehicle] = {}
        self.by_edge: dict[str, list[dict]] = {}
        self.lanes: dict[str, LaneWindow] = {}
        self.seen_batches: OrderedDict[str, None] = OrderedDict()
        self.sim_time = 0.0
        self.sim_stats: dict[str, Any] = {}
        self.advised_until: dict[str, float] = {}   # vid -> wall time, for the map
        self.batches = 0

    def is_duplicate(self, batch_id: str) -> bool:
        if batch_id in self.seen_batches:
            return True
        self.seen_batches[batch_id] = None
        while len(self.seen_batches) > 600:
            self.seen_batches.popitem(last=False)
        return False

    def apply_batch(self, sim_time: float, records: list[dict]) -> list[str]:
        """Update state from one batch; returns vids that disappeared."""
        self.sim_time = sim_time
        self.batches += 1
        seen = set()
        by_edge: dict[str, list[dict]] = {}
        lane_acc: dict[str, list[float]] = {}
        for r in records:
            vid = r["meta"]["vid"]
            seen.add(vid)
            v = self.vehicles.get(vid)
            if v is None:
                v = self.vehicles[vid] = Vehicle(rec=r)
            v.rec, v.last_seen = r, sim_time
            if r["speed"] < self.stalled_speed:
                v.stopped_since = v.stopped_since if v.stopped_since is not None else sim_time
            else:
                v.stopped_since = None
            if r["speed"] > self.close_speed:
                v.moving_since = v.moving_since if v.moving_since is not None else sim_time
            else:
                v.moving_since = None
            by_edge.setdefault(r["edge"], []).append(r)
            if not r["lane"].startswith(":"):
                lane_acc.setdefault(r["lane"], []).append(r["speed"])
        gone = [vid for vid in self.vehicles if vid not in seen]
        for vid in gone:
            del self.vehicles[vid]
        self.by_edge = by_edge
        for lane, speeds in lane_acc.items():
            w = self.lanes.setdefault(lane, LaneWindow())
            w.samples.append((sim_time, sum(speeds) / len(speeds), len(speeds)))
            w.last_seen = sim_time
        for lane in [l for l, w in self.lanes.items() if sim_time - w.last_seen > 120]:
            del self.lanes[lane]
        now = time.time()
        for vid in [v for v, t in self.advised_until.items() if t < now]:
            del self.advised_until[vid]
        return gone

    def lane_window(self, lane: str, seconds: float) -> list[tuple[float, float, int]]:
        w = self.lanes.get(lane)
        if not w:
            return []
        return [s for s in w.samples if s[0] > self.sim_time - seconds]
