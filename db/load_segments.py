"""Load road_segments (one document per lane, GeoJSON LineString) from the SUMO net.

Fixed reference data: replaces the collection content in a few insert_many calls.

    python db/load_segments.py
"""
from __future__ import annotations

import json
import sys

from common.mongo import get_db
from common.roadnet import load_net
from common.settings import config, path


def main() -> int:
    net = load_net()
    zones = json.loads(path(config()["sim"]["zones_file"]).read_text())
    docs = []
    for edge in net.getEdges():
        if edge.isSpecial():
            continue
        for lane in edge.getLanes():
            coords = []
            for x, y in lane.getShape():
                lon, lat = net.convertXY2LonLat(x, y)
                pt = [round(lon, 7), round(lat, 7)]
                if not coords or coords[-1] != pt:
                    coords.append(pt)
            if len(coords) < 2:
                continue
            docs.append({
                "lane_id": lane.getID(), "edge": edge.getID(), "index": lane.getIndex(),
                "name": edge.getName() or None, "zone": zones.get(edge.getID(), "other"),
                "length": round(lane.getLength(), 1), "speed_limit": round(lane.getSpeed(), 2),
                "geometry": {"type": "LineString", "coordinates": coords},
            })
    db = get_db("hanlane-init")
    db.road_segments.delete_many({})
    for i in range(0, len(docs), 2000):
        db.road_segments.insert_many(docs[i:i + 2000], ordered=False)
    print(f"road_segments: {len(docs)} lanes from {len(net.getEdges())} edges "
          f"({db.road_segments.count_documents({})} in Atlas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
