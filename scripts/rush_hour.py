"""Load test: x3 vehicles, then WRITE_MODE=single, through the gateway controls.

Phase 1: demand x3 with insert_many (batch).  Phase 2: x3 and insert_one per record (single).
Then restores batch mode and the original demand. Samples gateway stats every 5 s and prints
what happened to write time, backlog and loop latency. With DD_APP_KEY set, also prints which
Datadog monitors are alerting afterwards.

    python scripts/rush_hour.py [--factor 3] [--batch-minutes 2] [--single-minutes 3]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

import httpx

from common.settings import ROOT, settings


def sample(c: httpx.Client) -> dict:
    s = c.get("/api/stats").json()
    return {k: s.get(k) for k in ("vehicles", "write_mode", "last_write_ms", "pending_batches", "dropped_batches",
                                  "loop_p95", "scale")}


def phase(c: httpx.Client, name: str, minutes: float) -> list[dict]:
    rows, end = [], time.time() + minutes * 60
    while time.time() < end:
        r = sample(c)
        r["phase"] = name
        rows.append(r)
        print(f"{name:<14} vehicles={r['vehicles']:>4} write={r['write_mode']:<6} "
              f"last_write_ms={r['last_write_ms'] or 0:>7.0f} pending={r['pending_batches']:>3} "
              f"dropped={r['dropped_batches']:>3} loop_p95={r['loop_p95'] or 0:>6.0f}", flush=True)
        time.sleep(5)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--factor", type=float, default=3.0)
    ap.add_argument("--batch-minutes", type=float, default=2.0)
    ap.add_argument("--single-minutes", type=float, default=3.0)
    args = ap.parse_args()
    c = httpx.Client(base_url=settings().gateway_url, timeout=10)
    base = c.get("/api/stats").json().get("scale")
    if base is None:
        sys.exit("sim-bridge is not sending (no scale in gateway stats)")
    rows = phase(c, "baseline", 0.5)
    try:
        c.post("/api/control/scale", json={"scale": round(base * args.factor, 3)}).raise_for_status()
        rows += phase(c, f"x{args.factor:g} batch", args.batch_minutes)
        c.post("/api/control/write-mode", json={"mode": "single"}).raise_for_status()
        rows += phase(c, f"x{args.factor:g} single", args.single_minutes)
    finally:
        c.post("/api/control/write-mode", json={"mode": "batch"})
        c.post("/api/control/scale", json={"scale": base})
    rows += phase(c, "recovery", 1.0)

    print("\nsummary (max per phase)")
    for name in dict.fromkeys(r["phase"] for r in rows):
        ph = [r for r in rows if r["phase"] == name]
        print(f"  {name:<14} vehicles<={max(r['vehicles'] or 0 for r in ph):>4} "
              f"write_ms<={max(r['last_write_ms'] or 0 for r in ph):>7.0f} "
              f"pending<={max(r['pending_batches'] or 0 for r in ph):>3} "
              f"dropped={max(r['dropped_batches'] or 0 for r in ph):>3} "
              f"loop_p95<={max(r['loop_p95'] or 0 for r in ph):>6.0f}")
    (ROOT / "docs" / "rush_hour.json").write_text(json.dumps(rows, indent=1))
    if settings().dd_app_key:
        print("\nDatadog monitors:", flush=True)
        subprocess.run([sys.executable, str(ROOT / "scripts" / "dd_apply.py"), "--status"], cwd=ROOT)
    else:
        print("\nDD_APP_KEY not set: run `make dd-status` after adding it to see which monitors fired.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
