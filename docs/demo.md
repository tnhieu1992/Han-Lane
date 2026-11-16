# Demo script (6 minutes)

One incident on the Dragon Bridge, seen from three angles: the map, Datadog, and the scale story. The live part is in the middle; the load test is shown as a recording because monitors need a few minutes to fire.

## Before going on stage (T-15 min)

```bash
make agent-up
make demo                     # wait for "web http://localhost:5180"; 150 s simulated warm-up
open http://localhost:5180
make dd-status                # monitors OK, no leftover alerts
```

- Browser tab 1: http://localhost:5180 (zoomed on the bridge: **Về cầu Rồng**).
- Browser tab 2: Datadog dashboard "HanLane · real-time hazard warnings", last 15 minutes.
- Browser tab 3: Datadog APM traces, `service:hanlane-gateway resource_name:STALLED`.
- Video of the load test (`make rush`, recorded beforehand) ready to play.
- Backup video of the whole live section in case the network fails.

## Timeline

| Time | Screen | Say / do |
| --- | --- | --- |
| 0:00–0:45 | Slide: problem | Hanoi and HCMC traffic vs Da Nang; Da Nang already connects its traffic lights and cameras to a central system; Vietnam has an autonomous-vehicle sandbox. |
| 0:45–1:30 | Slide: architecture | Five parts, one closed loop, target under 1 s (V2N, 3GPP TS 22.185). |
| 1:30–3:15 | Live map | ~60 vehicles on real Da Nang streets (85% motorbikes). Click **Tạo sự cố · Cầu Rồng**: a vehicle brakes hard, stops, hazard lights on. Vehicles behind turn orange (slow down, change lane). Point at the panel: loop p95 ~200 ms. Then show `docs/ab_results.md` (advisories off vs on). Click **Dọn sự cố**. |
| 3:15–4:30 | Datadog | Open the trace of that incident: sim → gateway → `rules.fire` → MongoDB insert → worker → sim, one trace across the change stream. Dashboard: loop p50/p95, per-hop budget, change stream vs fast path, SLO always fed by the 1 Hz probe. Play the load-test recording: x3 traffic + `WRITE_MODE=single`, write time climbs, MongoDB monitor fires, switch back and it recovers. |
| 4:30–5:20 | Slide: scale | Measured numbers: ~100 documents/s write ceiling on the free tier, 249 bytes per record, 2 h TTL; what an M10+ cluster, sharding by zone and Atlas Stream Processing change. |
| 5:20–6:00 | Datadog MCP (optional) | Ask Datadog through MCP "why did latency rise just now?" (cut first if short on time; not set up in this repo). Closing line. |

## Numbers to quote

From `docs/results.md`: loop p95 ~200 ms (fast path) and change stream p95 ~540 ms on the shared tier; detection under 10 ms; `insert_many` of 50 docs p50 145 ms; write ceiling ~100 docs/s; 249 bytes per record on disk. A/B (same incident, same seed, 52 vehicles behind): harsh braking 15 → 6, emergency braking 9 → 5. Under x3 load with single writes, telemetry falls 11–24 s behind while the incident loop stays at ~200 ms.

After the load test, restart the sim before the next live run (demand takes minutes to fall back).

## Questions to expect

| Question | Short answer |
| --- | --- |
| Why not a regular collection for telemetry? | Time series compresses by bucket and is tuned for time-range queries and window functions; 249 bytes per record on disk including indexes (`docs/measure_m0.json`). |
| Why is the change stream not on telemetry? | Time series collections do not support change streams; a separate `events` collection also lets several consumers share one event stream. |
| Why a fast path if you have a change stream? | Measured change stream p95 on the shared tier was ~540 ms against a 400 ms hop budget. The gateway also posts the event to the worker; the change stream remains the durable path (the worker de-duplicates), and its lag is still a Datadog metric. |
| Where would real data come from? | City cameras and traffic lights for the roadside part, on-board units for telemetry. The simulator uses that schema. |
| Privacy? | Vehicle ids are pseudonyms per trip (a fixed id at 1 Hz identifies a person); raw telemetry expires after 2 h; only rollups stay. |
| Lane-level position? | Taken from SUMO here; in reality it needs lane-level positioning or on-board cameras. Stated as an assumption. |
| Why ~60 vehicles and not 300? | The measured write ceiling on the free tier is ~100 documents/s; 300 vehicles at 1 Hz needs a paid tier. That is the scale story. |
