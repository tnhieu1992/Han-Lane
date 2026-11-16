"""A/B: the same incident twice with the same seed, advisories off then on.

Each run fast-forwards (no telemetry) to 15 s before the incident, then runs in real time
through gateway and worker, triggers the incident on the Dragon Bridge at the same simulated
second, and records the vehicles behind it (within 150 m upstream): how many braked harder than
3 m/s^2, how many needed emergency braking (beyond the 4.5 m/s^2 normal maximum), smallest gap
and headway, collisions.

Needs gateway and worker running, and no other sim-bridge on port 8100.

    python scripts/ab_run.py [--seed 42] [--incident-at 300] [--after 120]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime

import httpx

from common.settings import ROOT, settings

OUT = ROOT / "docs"
ROWS = [
    ("followers", "Vehicles observed within 150 m behind the incident"),
    ("harsh_brakers", "Braked harder than 3 m/s²"),
    ("emergency_brakers", "Emergency braking (beyond 4.5 m/s²)"),
    ("min_gap_m", "Smallest gap to the vehicle ahead (m, ignoring side-by-side sublane overlaps)"),
    ("min_headway_s", "Smallest time headway (s)"),
    ("collisions", "Collisions"),
    ("warned_followers", "Followers that received a warning"),
    ("advisory_events_applied", "Advisory notices applied (initial + newcomers)"),
    ("loop_p95_ms", "Loop p95 t0→t5 (ms)"),
]


def run(mode: str, args) -> dict:
    gw = settings().gateway_url
    httpx.post(f"{gw}/api/control/reset", timeout=5).raise_for_status()
    out = OUT / f"ab_{mode}.json"
    out.unlink(missing_ok=True)
    cmd = [
        sys.executable, "-m", "common.envrun", "hanlane-sim",
        str(ROOT / ".venv" / "bin" / "ddtrace-run"), sys.executable, "-m", "sim.bridge",
        "--seed", str(args.seed), "--stable-ids",
        "--fast-until", str(args.incident_at - 15), "--incident-at", str(args.incident_at),
        "--end", str(args.incident_at + args.after), "--advisories", mode, "--ab-out", str(out),
    ]
    if args.scale:
        cmd += ["--scale", str(args.scale)]
    print(f"run advisories={mode}: sim {args.incident_at - 15}s fast, then {15 + args.after}s real time ...",
          flush=True)
    with open(ROOT / "logs" / f"ab_{mode}.out", "w") as log:
        subprocess.run(cmd, cwd=ROOT, check=True, stdout=log, stderr=subprocess.STDOUT)
    return json.loads(out.read_text())


def fmt(v) -> str:
    return "–" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--incident-at", type=int, default=300)
    ap.add_argument("--after", type=int, default=120)
    ap.add_argument("--scale", type=float)
    args = ap.parse_args()
    res = {mode: run(mode, args) for mode in ("off", "on")}

    lines = [
        f"## A/B: advisories off vs on ({datetime.now():%Y-%m-%d %H:%M})",
        "",
        f"Same seed ({args.seed}), same incident (vehicle `{res['off']['incident'].get('vid')}` on edge "
        f"`{res['off']['incident'].get('edge')}` at t={args.incident_at}s), observed for {args.after}s.",
        "",
        "| Metric | Advisories off | Advisories on |",
        "| --- | --- | --- |",
    ]
    for key, label in ROWS:
        lines.append(f"| {label} | {fmt(res['off'].get(key))} | {fmt(res['on'].get(key))} |")
    same_vehicle = res["off"]["incident"].get("vid") == res["on"]["incident"].get("vid")
    lines += ["", f"Same incident vehicle in both runs: {'yes' if same_vehicle else 'NO, compare with care'}.", ""]
    text = "\n".join(lines)
    print("\n" + text)
    (OUT / "ab_results.md").write_text(text + "\n")
    print(f"saved docs/ab_results.md, docs/ab_off.json, docs/ab_on.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
