"""Collection and index specs, declared once. TTL values come from config/hanlane.toml."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from common.settings import config


@dataclass
class IndexSpec:
    name: str
    keys: list[tuple[str, Any]]
    unique: bool = False
    ttl: int | None = None  # expireAfterSeconds


@dataclass
class CollectionSpec:
    name: str
    timeseries: dict[str, str] | None = None
    ttl: int | None = None  # time series collection-level expireAfterSeconds
    indexes: list[IndexSpec] = field(default_factory=list)


def specs() -> list[CollectionSpec]:
    ttl = config()["ttl"]
    return [
        CollectionSpec(
            "telemetry",
            timeseries={"timeField": "ts", "metaField": "meta", "granularity": "seconds"},
            ttl=ttl["telemetry_seconds"],
            indexes=[
                IndexSpec("vid_ts", [("meta.vid", 1), ("ts", -1)]),
                IndexSpec("loc_2dsphere", [("loc", "2dsphere")]),
            ],
        ),
        CollectionSpec(
            "rsu_readings",
            timeseries={"timeField": "ts", "metaField": "meta", "granularity": "seconds"},
            ttl=ttl["rsu_readings_seconds"],
            indexes=[IndexSpec("rsu_ts", [("meta.rsu_id", 1), ("ts", -1)])],
        ),
        CollectionSpec(
            "events",
            indexes=[
                IndexSpec("ts_desc", [("ts", -1)]),
                IndexSpec("loc_2dsphere", [("loc", "2dsphere")]),
                IndexSpec("type_zone_ts", [("type", 1), ("zone", 1), ("ts", -1)]),
                IndexSpec("status", [("status", 1)]),
                # Per-document expiry: probes short, incidents 7 days.
                IndexSpec("expire_at_ttl", [("expireAt", 1)], ttl=0),
            ],
        ),
        CollectionSpec(
            "advisories",
            indexes=[
                IndexSpec("event_id", [("event_id", 1)]),
                IndexSpec("ts_ttl", [("ts", -1)], ttl=ttl["advisories_seconds"]),
            ],
        ),
        CollectionSpec(
            "road_segments",
            indexes=[
                IndexSpec("geometry_2dsphere", [("geometry", "2dsphere")]),
                IndexSpec("edge", [("edge", 1)]),
                IndexSpec("lane_id", [("lane_id", 1)]),
            ],
        ),
        CollectionSpec(
            "rollups_1m",
            indexes=[
                IndexSpec("edge_minute_unique", [("edge", 1), ("minute", 1)], unique=True),
                IndexSpec("zone_minute", [("zone", 1), ("minute", -1)]),
                IndexSpec("minute_ttl", [("minute", 1)], ttl=ttl["rollups_seconds"]),
            ],
        ),
    ]
