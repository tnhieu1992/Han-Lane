"""Measure what the Atlas M0 limits mean for HanLane.

1. Storage: telemetry on-disk size per record and compression ratio -> TTL that fits 300 vehicles.
2. Bytes in: BSON and zstd-compressed bytes per batch -> GB per 7 days.
3. Write pattern: 150 insert_one per second vs one insert_many of 300 documents per second
   (30 s each, scratch time series collection, dropped afterwards). Reports latency, achieved
   rate, longest stall and serverStatus opcounters deltas.

Run it with the simulator stopped so the counters are not mixed with live traffic.

    python scripts/measure_m0.py [--seconds 30] [--skip-write-test]
    python scripts/measure_m0.py --skip-storage --seconds 12 --sweep 50,100,200   # find the knee
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import bson

from common.mongo import get_db
from common.settings import ROOT, config

try:  # PyMongo's own zstd binding
    from backports import zstd as _zstd
except ImportError:  # pragma: no cover
    from compression import zstd as _zstd  # type: ignore[no-redef]

M0_STORAGE_MB = 512
TELEMETRY_BUDGET_MB = 300   # leave room for indexes, events, rollups and the other database
SCRATCH = "m0_probe"


def pct(values, q):
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(q * (len(s) - 1))))], 1) if s else None


def fake_record(i: int, ts: datetime) -> dict:
    lon, lat = 108.2272 + random.uniform(-0.01, 0.01), 16.0612 + random.uniform(-0.01, 0.01)
    return {
        "ts": ts, "t0": datetime.now(timezone.utc),
        "meta": {"vid": f"moto-{i:06x}", "type": "motorbike"},
        "loc": {"type": "Point", "coordinates": [lon, lat]}, "lon": lon, "lat": lat,
        "zone": "cau-rong", "edge": "295386151", "lane": "295386151_0", "pos": round(random.uniform(0, 580), 1),
        "speed": round(random.uniform(0, 16), 2), "accel": round(random.uniform(-3, 2), 2),
        "heading": round(random.uniform(0, 360), 1), "latOffset": round(random.uniform(-1, 1), 2),
        "leaderGap": round(random.uniform(2, 60), 1),
        "signals": {"brake": False, "hazard": False}, "nextTls": {"state": "G", "dist": 140.0},
    }


def server_counters(db) -> dict:
    s = db.client.admin.command("serverStatus")
    return {
        "insert": s["opcounters"]["insert"], "command": s["opcounters"]["command"],
        "query": s["opcounters"]["query"], "numRequests": s["network"]["numRequests"],
        "bytesIn": s["network"]["bytesIn"],
    }


def delta(a: dict, b: dict) -> dict:
    return {k: b[k] - a[k] for k in a}


# ---------------------------------------------------------------------------- 1. storage
def storage_report(db) -> dict:
    out: dict = {}
    try:
        cs = next(db.telemetry.aggregate([{"$collStats": {"storageStats": {}}}]))["storageStats"]
        out["storageSize_mb"] = round(cs.get("storageSize", 0) / 1e6, 2)
        out["dataSize_mb"] = round(cs.get("size", 0) / 1e6, 2)
        out["indexSize_mb"] = round(cs.get("totalIndexSize", 0) / 1e6, 2)
        out["buckets"] = (cs.get("timeseries") or {}).get("bucketCount")
    except Exception as e:  # noqa: BLE001 - report whatever the tier allows
        out["collStats_error"] = str(e)
    n = db.telemetry.count_documents({})
    out["records"] = n
    sample = list(db.telemetry.aggregate([{"$sample": {"size": 300}}]))
    if sample:
        sizes = [len(bson.encode(d)) for d in sample]
        out["avg_record_bson_bytes"] = round(statistics.mean(sizes), 1)
    if n and "storageSize_mb" in out and out.get("avg_record_bson_bytes"):
        logical = n * out["avg_record_bson_bytes"]
        on_disk = (out["storageSize_mb"] + out["indexSize_mb"]) * 1e6
        out["disk_bytes_per_record"] = round(on_disk / n, 1)
        out["compression_ratio"] = round(logical / on_disk, 2) if on_disk else None
        per_rec = on_disk / n
        for vehicles in (50, 300):
            out[f"mb_per_hour_{vehicles}_vehicles"] = round(vehicles * 3600 * per_rec / 1e6, 1)
        # TTL deletes whole buckets ~17 min after the cut-off at 1 Hz per vehicle.
        ttl_s = TELEMETRY_BUDGET_MB * 1e6 / (300 * per_rec) - 17 * 60
        out["suggested_ttl_seconds_300_vehicles"] = int(max(600, ttl_s))
    return out


# ---------------------------------------------------------------------------- 2. bytes
def bytes_report(db) -> dict:
    sample = list(db.telemetry.aggregate([{"$sample": {"size": 300}}, {"$project": {"_id": 0}}]))
    docs = sample or [fake_record(i, datetime.now(timezone.utc)) for i in range(300)]
    out = {}
    for vehicles in (50, 300):
        batch = (docs * (vehicles // len(docs) + 1))[:vehicles]
        raw = len(bson.encode({"insert": "telemetry", "documents": batch}))
        comp = len(_zstd.compress(bson.encode({"insert": "telemetry", "documents": batch})))
        out[f"batch_{vehicles}"] = {
            "bson_bytes": raw, "zstd_bytes": comp, "ratio": round(raw / comp, 2),
            "gb_per_7d_if_24h": round(comp * 86400 * 7 / 1e9, 2),
            "gb_per_7d_if_3h_per_day": round(comp * 3600 * 3 * 7 / 1e9, 2),
        }
    return out


# ---------------------------------------------------------------------------- 3. writes
def write_test(db, seconds: int, sweep: list[int] | None = None) -> dict:
    db.drop_collection(SCRATCH)
    db.create_collection(SCRATCH, timeseries={"timeField": "ts", "metaField": "meta", "granularity": "seconds"})
    coll = db[SCRATCH]
    results = {}
    try:
        if sweep:
            for size in sweep:
                results[f"insert_many_{size}_per_s"] = run_many(db, coll, seconds, size)
                time.sleep(3)
        else:
            results["single_150_per_s"] = run_single(db, coll, seconds, 150)
            time.sleep(3)
            results["insert_many_300_per_s"] = run_many(db, coll, seconds, 300)
    finally:
        db.drop_collection(SCRATCH)
    return results


def run_single(db, coll, seconds: int, rate: int) -> dict:
    lat, done, errors = [], [], []
    lock = threading.Lock()

    def one(i: int, ts: datetime) -> None:
        start = time.perf_counter()
        try:
            coll.insert_one(fake_record(i, ts))
            end = time.perf_counter()
            with lock:
                lat.append((end - start) * 1000)
                done.append(end)
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(str(e)[:120])

    before = server_counters(db)
    t_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=64) as pool:
        for sec in range(seconds):
            ts = datetime.now(timezone.utc)
            target = t_start + sec
            for i in range(rate):
                pool.submit(one, i, ts + timedelta(milliseconds=i))
            sleep = target + 1 - time.perf_counter()
            if sleep > 0:
                time.sleep(sleep)
    elapsed = time.perf_counter() - t_start
    after = server_counters(db)
    return summarize(lat, done, errors, elapsed, seconds * rate, delta(before, after), t_start)


def run_many(db, coll, seconds: int, size: int) -> dict:
    lat, done, errors = [], [], []
    before = server_counters(db)
    t_start = time.perf_counter()
    for sec in range(seconds):
        ts = datetime.now(timezone.utc)
        docs = [fake_record(i, ts) for i in range(size)]
        start = time.perf_counter()
        try:
            coll.insert_many(docs, ordered=False)
            end = time.perf_counter()
            lat.append((end - start) * 1000)
            done.append(end)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e)[:120])
        sleep = t_start + sec + 1 - time.perf_counter()
        if sleep > 0:
            time.sleep(sleep)
    elapsed = time.perf_counter() - t_start
    after = server_counters(db)
    out = summarize(lat, done, errors, elapsed, seconds, delta(before, after), t_start)
    out["documents"] = seconds * size - len(errors) * size
    return out


def summarize(lat, done, errors, elapsed, attempted, counters, t_start) -> dict:
    done = sorted(done)
    gaps = [b - a for a, b in zip([t_start] + done, done)]
    return {
        "attempted_ops": attempted, "ok_ops": len(lat), "errors": len(errors),
        "error_sample": errors[:2], "elapsed_s": round(elapsed, 1),
        "achieved_ops_per_s": round(len(lat) / elapsed, 1),
        "latency_ms": {"p50": pct(lat, 0.5), "p95": pct(lat, 0.95), "max": round(max(lat), 1) if lat else None},
        "longest_gap_between_completions_s": round(max(gaps), 2) if gaps else None,
        "server_counters_delta": counters,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--skip-write-test", action="store_true")
    ap.add_argument("--skip-storage", action="store_true")
    ap.add_argument("--sweep", help="comma-separated insert_many sizes, e.g. 50,100,200 (replaces the default test)")
    args = ap.parse_args()
    db = get_db("hanlane-measure")
    report = {"measured_at": datetime.now(timezone.utc).isoformat(),
              "current_ttl_seconds": config()["ttl"]["telemetry_seconds"]}
    if not args.skip_storage:
        print("1/3 storage ...", flush=True)
        report["storage"] = storage_report(db)
        print(json.dumps(report["storage"], indent=2))
        print("2/3 bytes per batch ...", flush=True)
        report["bytes"] = bytes_report(db)
        print(json.dumps(report["bytes"], indent=2))
    if not args.skip_write_test:
        print(f"3/3 write test, 2 x {args.seconds}s on '{SCRATCH}' (dropped afterwards) ...", flush=True)
        sweep = [int(x) for x in args.sweep.split(",")] if args.sweep else None
        report["writes" if not sweep else "sweep"] = write_test(db, args.seconds, sweep)
        print(json.dumps(report.get("writes") or report.get("sweep"), indent=2))
    out = ROOT / "docs" / ("measure_m0_sweep.json" if args.sweep else "measure_m0.json")
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nsaved {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
