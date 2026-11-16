"""Per-edge, per-minute rollup of telemetry into rollups_1m with $merge.

Time series -> regular collection is the only direction $merge allows. Junction-internal edges
(ids starting with ':') are skipped. whenMatched "merge" so a future RSU rollup on the same
{edge, minute} key adds its fields instead of overwriting.

    python db/rollup.py                 # the last completed minute
    python db/rollup.py --minutes 10    # backfill the last 10 completed minutes
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from pymongo.database import Database

from common.mongo import get_db
from common.timeutil import utcnow


def window(minutes: int) -> tuple:
    """The last `minutes` completed minutes: [start, end) with end = start of the current minute."""
    end = utcnow().replace(second=0, microsecond=0)
    return end - timedelta(minutes=minutes), end


def pipeline(start, end) -> list[dict]:
    return [
        {"$match": {"ts": {"$gte": start, "$lt": end}}},          # bucket-level filter on the time field
        {"$match": {"edge": {"$not": {"$regex": "^:"}}}},
        {"$project": {"_id": 0, "ts": 1, "edge": 1, "zone": 1, "speed": 1, "vid": "$meta.vid"}},
        {"$group": {
            "_id": {"edge": "$edge", "minute": {"$dateTrunc": {"date": "$ts", "unit": "minute"}}},
            "zone": {"$first": "$zone"},
            "avgSpeed": {"$avg": "$speed"},
            "minSpeed": {"$min": "$speed"},
            "samples": {"$sum": 1},
            "vids": {"$addToSet": "$vid"},
        }},
        {"$project": {
            "_id": 0, "edge": "$_id.edge", "minute": "$_id.minute", "zone": 1,
            "avgSpeed": {"$round": ["$avgSpeed", 2]}, "minSpeed": 1, "samples": 1,
            "vehicles": {"$size": "$vids"},
        }},
        {"$merge": {"into": "rollups_1m", "on": ["edge", "minute"],
                    "whenMatched": "merge", "whenNotMatched": "insert"}},
    ]


def run(db: Database, minutes: int = 1, count: bool = False) -> int | None:
    """Merge the last `minutes` completed minutes. Each minute is written once (~1 document per
    active edge): on M0 every merged document counts against the ~100 documents/s write budget,
    so re-merging overlapping windows would throttle telemetry inserts."""
    start, end = window(minutes)
    db.telemetry.aggregate(pipeline(start, end))
    return db.rollups_1m.count_documents({"minute": {"$gte": start, "$lt": end}}) if count else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=int, default=1)
    args = ap.parse_args()
    db = get_db("hanlane-rollup")
    n = run(db, args.minutes, count=True)
    print(f"rollups_1m: {n} edge-minutes in the last {args.minutes} completed minute(s)")
    top = list(db.rollups_1m.find({}, {"_id": 0, "edge": 1, "zone": 1, "minute": 1, "avgSpeed": 1, "vehicles": 1})
               .sort([("minute", -1), ("vehicles", -1)]).limit(5))
    for row in top:
        print(f"  {row['minute']:%H:%M} {row['zone']:<16} edge {row['edge']:<28} "
              f"avg {row['avgSpeed']:>5} m/s  vehicles {row['vehicles']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
