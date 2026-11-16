"""Build the SUMO scenario for central Da Nang (both banks of the Han river).

1. Download OSM data for the bbox in config/hanlane.toml (OSM API), cached in sim/osm/.
2. netconvert with junction joining, guessed traffic lights, cars/motorbikes/buses only.
3. Vehicle types (85% motorbike, 12% car, 3% bus) in sim/vtypes.add.xml.
4. Random trips plus steady flows across the Dragon Bridge (Cau Rong) so incidents have followers.
5. Edge -> zone map and scenario metadata (default incident edge) in sim/net/.

    python sim/build_net.py [--force-download]
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
from pathlib import Path

import sumo
import sumolib

from common.settings import ROOT, config, path

OSM_DIR = ROOT / "sim" / "osm"
NET_DIR = ROOT / "sim" / "net"
BIN = Path(sys.executable).parent
TOOLS = Path(sumo.SUMO_HOME) / "tools"

VTYPES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!-- Explicit vehicle types: SUMO defaults (motorbike decel 10 m/s^2) are too harsh. -->
<additional>
  <vTypeDistribution id="mix">
    <vType id="motorbike" vClass="motorcycle" probability="0.85" length="1.9" width="0.75" minGap="1.0"
           accel="3.0" decel="4.5" emergencyDecel="8.0" maxSpeed="16.7" speedFactor="normc(1,0.1,0.6,1.3)"
           latAlignment="arbitrary" minGapLat="0.3" maxSpeedLat="1.0" sigma="0.5" color="1,0.8,0"/>
    <vType id="car" vClass="passenger" probability="0.12" length="4.5" width="1.8" minGap="2.5"
           accel="2.6" decel="4.5" emergencyDecel="9.0" maxSpeed="16.7" speedFactor="normc(1,0.1,0.6,1.3)"
           latAlignment="center" minGapLat="0.6" sigma="0.5" color="0.3,0.6,1"/>
    <vType id="bus" vClass="bus" probability="0.03" length="12.0" width="2.5" minGap="3.0"
           accel="1.2" decel="4.0" emergencyDecel="7.0" maxSpeed="13.9"
           latAlignment="center" minGapLat="0.6" sigma="0.5" color="0.2,0.8,0.3"/>
  </vTypeDistribution>
</additional>
"""


def download_osm(bbox: list[float], force: bool) -> Path:
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    out = OSM_DIR / "danang.osm.xml"
    if out.exists() and not force:
        print(f"osm: cached {out} ({out.stat().st_size / 1e6:.1f} MB)")
        return out
    s, w, n, e = bbox
    # Official OSM API (bbox well under its 0.25 deg^2 / 50k node limits). curl -4 because
    # some networks have no IPv6 route and Python's urllib does not fall back.
    url = f"https://api.openstreetmap.org/api/0.6/map?bbox={w},{s},{e},{n}"
    print("osm: downloading from api.openstreetmap.org ...")
    subprocess.run(
        ["curl", "-sS", "-4", "-f", "-m", "180", "-A", "hanlane-build-net/0.1", "-o", str(out), url], check=True
    )
    print(f"osm: saved {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return out


def run(cmd: list[str]) -> None:
    print("$", " ".join(str(c) for c in cmd[:6]), "..." if len(cmd) > 6 else "")
    subprocess.run([str(c) for c in cmd], check=True)


def netconvert(osm: Path, net: Path) -> None:
    run([
        BIN / "netconvert",
        "--osm-files", osm,
        "--output-file", net,
        "--geometry.remove", "--ramps.guess", "--junctions.join",
        "--tls.guess-signals", "--tls.discard-simple", "--tls.join", "--tls.default-type", "static",
        "--keep-edges.by-vclass", "passenger,motorcycle,bus",
        "--remove-edges.by-type", "highway.service,highway.track,highway.unsurfaced,highway.footway,"
                                  "highway.pedestrian,highway.path,highway.cycleway,highway.steps",
        "--remove-edges.isolated",
        "--no-turnarounds.except-deadend",
        "--output.street-names", "--output.original-names",
        "--junctions.corner-detail", "5",
        "--no-warnings",
    ])


def write_vtypes(dest: Path) -> None:
    dest.write_text(VTYPES_XML)
    print(f"vtypes: {dest}")


def find_bridge_edges(net: sumolib.net.Net) -> list[sumolib.net.edge.Edge]:
    edges = [e for e in net.getEdges() if not e.isSpecial() and "rồng" in (e.getName() or "").lower()]
    return sorted(edges, key=lambda e: -e.getLength())


def walk(edge, direction: str, min_dist: float):
    """Follow the longest connected chain upstream/downstream until min_dist is covered."""
    cur, dist, seen = edge, 0.0, {edge.getID()}
    while dist < min_dist:
        nxt = cur.getIncoming() if direction == "up" else cur.getOutgoing()
        cands = [e for e in nxt if not e.isSpecial() and e.getID() not in seen and e.allows("passenger")]
        if not cands:
            break
        cur = max(cands, key=lambda e: (e.getLaneNumber(), e.getLength()))
        seen.add(cur.getID())
        dist += cur.getLength()
    return cur


def write_bridge_flows(net, bridge_edges, dest: Path) -> list[dict]:
    flows, info = [], []
    # The first bridge edge is the default incident edge: denser flow so an incident has followers.
    rates = [1.0, 0.25]
    for i, e in enumerate(bridge_edges[:2]):
        src, dst = walk(e, "up", 250), walk(e, "down", 250)
        if src.getID() == e.getID() or dst.getID() == e.getID():
            continue
        flows.append(
            f'  <flow id="bridge{i}" type="mix" begin="0" end="86400" period="exp({rates[i]})" '
            f'from="{src.getID()}" via="{e.getID()}" to="{dst.getID()}" departLane="best" departSpeed="max"/>'
        )
        info.append({"bridge_edge": e.getID(), "from": src.getID(), "to": dst.getID()})
    dest.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n' + "\n".join(flows) + "\n</routes>\n")
    print(f"flows: {len(flows)} bridge flows -> {dest}")
    return info


def random_trips(net_file: Path, vtypes: Path, route_file: Path, seed: int) -> None:
    trips = NET_DIR / "danang.trips.xml"
    run([
        sys.executable, TOOLS / "randomTrips.py",
        "-n", net_file, "-o", trips, "-r", route_file,
        "--seed", str(seed), "--begin", "0", "--end", "7200", "--period", "4.0",
        "--fringe-factor", "5", "--min-distance", "600",
        "--validate",
        "--trip-attributes", 'type="mix" departLane="best" departSpeed="max"',
        "--additional-files", vtypes,
    ])
    trips.unlink(missing_ok=True)
    # duarouter copies the vTypes into the route file; vtypes.add.xml stays the only definition.
    with gzip.open(route_file, "rt") as f:
        text = f.read()
    text = re.sub(r"\s*<vTypeDistribution.*?</vTypeDistribution>", "", text, flags=re.S)
    text = re.sub(r"\s*<vType\b[^>]*/>", "", text)
    with gzip.open(route_file, "wt") as f:
        f.write(text)


def edge_zones(net: sumolib.net.Net, zones: list[dict]) -> dict[str, str]:
    out = {}
    for e in net.getEdges():
        if e.isSpecial():
            continue
        shape = e.getShape()
        x, y = shape[len(shape) // 2]
        lon, lat = net.convertXY2LonLat(x, y)
        out[e.getID()] = next(
            (z["name"] for z in zones if z["bbox"][0] <= lon <= z["bbox"][2] and z["bbox"][1] <= lat <= z["bbox"][3]),
            "other",
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-download", action="store_true")
    args = ap.parse_args()
    cfg = config()
    NET_DIR.mkdir(parents=True, exist_ok=True)

    osm = download_osm(cfg["sim"]["bbox"], args.force_download)
    net_file = path(cfg["sim"]["net_file"])
    netconvert(osm, net_file)
    vtypes = path(cfg["sim"]["vtypes_file"])
    write_vtypes(vtypes)

    net = sumolib.net.readNet(str(net_file), withInternal=False)
    edges = [e for e in net.getEdges() if not e.isSpecial()]
    tls = net.getTrafficLights()
    print(f"net: {len(net.getNodes())} nodes, {len(edges)} edges, {len(tls)} traffic lights, "
          f"{sum(e.getLength() for e in edges) / 1000:.1f} km of road")

    bridges = find_bridge_edges(net)
    if not bridges:
        print("WARNING: no edge named 'Rồng' found; incident default edge must be set by hand")
    for b in bridges[:4]:
        print(f"bridge edge: {b.getID()} '{b.getName()}' {b.getLength():.0f} m, {b.getLaneNumber()} lanes")

    route_file = path(cfg["sim"]["route_file"])
    random_trips(net_file, vtypes, route_file, cfg["sim"]["seed"])
    flows_file = NET_DIR / "bridge.flows.xml"
    flow_info = write_bridge_flows(net, bridges, flows_file)

    zones = edge_zones(net, cfg["zones"])
    path(cfg["sim"]["zones_file"]).write_text(json.dumps(zones))
    counts: dict[str, int] = {}
    for z in zones.values():
        counts[z] = counts.get(z, 0) + 1
    print("zones:", counts)

    # Default incident edge: the longest multi-lane bridge edge (room for followers and lane change).
    multi = [b for b in bridges if b.getLaneNumber() >= 2] or bridges
    meta = {
        "incident_edge": multi[0].getID() if multi else "",
        "bridge_edges": [b.getID() for b in bridges],
        "bridge_flows": flow_info,
        "flows_file": str(flows_file.relative_to(ROOT)),
    }
    (NET_DIR / "meta.json").write_text(json.dumps(meta, indent=2))
    print("meta:", json.dumps(meta))

    with gzip.open(route_file, "rt") as f:
        n_veh = sum(1 for line in f if "<vehicle " in line)
    print(f"routes: {n_veh} random-trip vehicles over 2 h")
    return 0


if __name__ == "__main__":
    sys.exit(main())
