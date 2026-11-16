"""Create or clear an incident (stopped vehicle with hazard lights) through the gateway,
the same path the web buttons use.

    python scripts/incident.py                 # Dragon Bridge (default edge)
    python scripts/incident.py --edge 123456#2
    python scripts/incident.py --clear
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

from common.settings import settings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--edge", help="SUMO edge id (default: Dragon Bridge from sim/net/meta.json)")
    ap.add_argument("--clear", action="store_true", help="let the incident vehicle drive on")
    args = ap.parse_args()
    url = settings().gateway_url
    if args.clear:
        resp = httpx.post(f"{url}/api/control/clear", timeout=6)
    else:
        resp = httpx.post(f"{url}/api/control/incident", json={"edge": args.edge}, timeout=6)
    print(json.dumps(resp.json(), indent=2))
    return 0 if resp.status_code < 400 and "error" not in resp.json() else 1


if __name__ == "__main__":
    sys.exit(main())
