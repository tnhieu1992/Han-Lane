# Measurements

All numbers measured on 2026-11-15/16 on one laptop (every service on the same machine, so t0…t5 share a clock), Atlas shared tier `workshopai` (MongoDB 8.0.37), ~60 vehicles unless stated. Raw data: `docs/measure_m0*.json`, `docs/ab_*.json`, `docs/rush_hour.json`.

## Feedback loop t0 → t5 (probes, 120 most recent)

| Hop | Budget (ms) | Change stream only: p50 / p95 | With fast path: p50 / p95 |
| --- | --- | --- | --- |
| t0→t1 sim read → gateway receives | 50 | 2.7 / 5.6 | 2.4 / 6.7 |
| t1→t3 rules + recipients | 30 | 1.1 / 2.5 | 0.9 / 2.3 |
| t3→t4 gateway → worker | 400 | 74.6 / **333.8** (later window: 86 / **539**) | 2.0 / 5.7 |
| t4→send worker → command sent | 100 | 0.2 / 0.5 | 0.3 / 0.7 |
| send→t5 command → applied on the vehicle | 200 | 116 / 126 | 190 / 196 |
| **Total** | 780 | 197 / 399 (later: 196 / 597) | **197 / 201** |

The total is bounded below by the 0.2 s SUMO step: t0 is read on a step boundary and commands are applied at the start of the next step, so a faster t3→t4 shifts time into "send→t5" rather than shortening the loop.

## MongoDB Atlas (shared tier)

| What | Result |
| --- | --- |
| Telemetry on disk | 249 bytes per record including indexes (data 0.9 MB + indexes 2.5 MB for 13,789 records; the `loc` 2dsphere index is the largest part) |
| Logical size | 420 bytes BSON per record, compression ratio 1.69 including indexes |
| Storage per hour | 45 MB at 50 vehicles, 269 MB at 300 vehicles |
| Bytes in (zstd) | 5.2 KB per batch of 50 (3.8x), 26 KB per batch of 300 (4.6x); 50 vehicles 24/7 = 3.2 GB per 7 days |
| 150 `insert_one`/s for 30 s | achieved 70.5/s, p50 894 ms, max 50.8 s; `opcounters.insert` +4500 |
| `insert_many` 50 docs/s | p50 145 ms, p95 227 ms |
| `insert_many` 100 docs/s | p50 915 ms |
| `insert_many` 200 docs/s | p50 936 ms |
| `insert_many` 300 docs/s for 30 s | p50 1995 ms, only 0.5 batches/s; `opcounters.insert` +9000 (counts documents) |
| Conclusion | Throttle behaves like ~100 documents per second; batching saves round trips, not quota. TTL 7200 s kept (fleet < 100 vehicles). |

## A/B: advisories off vs on

Seed 42, incident on the Dragon Bridge (edge `295386151`) at t = 300 s, same incident vehicle in both runs, 120 s observed, 52 vehicles passed within 150 m behind it.

| Metric | Off | On, warnings at detection only | On, + newcomers while open |
| --- | --- | --- | --- |
| Braked harder than 3 m/s² | 15 | 13 | **6** |
| Emergency braking (beyond 4.5 m/s²) | 9 | 8 | **5** |
| Followers that received a warning | 0 | (few, not tracked) | 43 of 52 |
| Collisions | 0 | 0 | 0 |
| Loop p95 (ms) | 200 | 201 | 202 |

Warning only the vehicles present at detection barely helps, because most followers arrive later. Warning newcomers while the stall is open cuts harsh braking by 60% and emergency braking by 44% in this run. One seed only: a direction, not a significance test. Minimum gap/headway are not reported: in a stopped queue they are 0 by construction (bumper to bumper at `minGap`), and with the sublane model SUMO reports negative gaps for vehicles partly alongside.

## Load test (`make rush`)

30 s baseline, then demand x3 for 2 min with `insert_many`, then x3 with `WRITE_MODE=single` for 3 min, then back to batch and x1. Gateway stats sampled every 5 s (`docs/rush_hour.json`).

| Phase | Vehicles (max) | Telemetry write per batch (max) | Batches waiting (max) | Batches dropped | Loop p95 (max) |
| --- | --- | --- | --- | --- | --- |
| Baseline, batch | 63 | 174 ms | 0 | 0 | 202 ms |
| x3, batch | 150 | 1,406 ms (jumps from ~130 ms to ~1,000 ms once past ~100 vehicles) | 1 | 0 | 204 ms |
| x3, single | 228 | 23,955 ms | 60 (queue cap) | 103 | 211 ms |
| Back to batch, x1 (first minute) | 214 | ~1,000 ms | 60 | 121 | 255 ms |

- The write ceiling shows up exactly where `measure_m0.py` predicted (~100 documents/s): batch writes stay ~1 s per batch above 100 vehicles, and `insert_one` per record falls minutes behind and drops data.
- The incident loop stays at ~200 ms p95 throughout: events have their own writer pool and the fast path, so a telemetry backlog does not delay warnings.
- Recovery is slow: vehicles already loaded at x3 keep entering the network for several minutes, and the backlog drains only once the fleet is under ~90 vehicles. For the demo, restart the sim after the load test.
- Monitors, second run on 2026-11-16 (`make rush`, then `make dd-status` a minute later):

  | Monitor | State | Why |
  | --- | --- | --- |
  | Telemetry ingest lag above 5 s | **Alert** | t0→t2 average went from 0.2–0.5 s to 35–74 s |
  | Stalled vehicles spike in a zone | **Alert** | x3 demand gridlocks Son Tra and the Dragon Bridge (no teleport): 14 and 8 STALLED without hazard lights |
  | Feedback loop p95 above 800 ms | OK | loop p95 stayed 199–219 ms |
  | Gateway MongoDB p95 above 1.5 s | OK | individual writes do not get slower; the queue does |
  | Telemetry received dropped, gateway no data, error rate | OK | the gateway keeps receiving every batch |

- The design doc's original "PyMongo p95 > 300 ms" monitor was in Alert during normal running (single writes take 0.25–1.1 s at p95 on the shared tier) and did not separate overload from normal; it was replaced (see `docs/decisions.md`).
