# Decisions that refine CLAUDE.md

CLAUDE.md stays the source of truth; these are the places where the implementation had to be more specific.

| Topic | Decision | Why |
| --- | --- | --- |
| `events` TTL | Field `expireAt` with a TTL index (`expireAfterSeconds: 0`). Incidents expire after 7 days, probes after 1 hour (`config/hanlane.toml [ttl]`). The `{ts: -1}` index stays as a normal index. | One probe per second kept for 7 days is ~600k documents, too much for the 512 MB M0 limit. |
| `rollups_1m` TTL | Extra single-field index `{minute: 1}` with the 3-day TTL. | The compound indexes cannot carry a TTL. |
| PyMongo version | `pymongo[zstd]>=4.17,<4.18`. | ddtrace 3.x patches `Server.run_operation`, which PyMongo 4.18+ removed; without the patch there are no MongoDB spans. |
| zstd | PyMongo 4.17 uses `backports.zstd` (installed by the `[zstd]` extra), not `zstandard`. | Driver change. |
| Datadog site | Read from `DD_SITE` in `.env`. The account in use is on `datadoghq.com` (US1), not `ap1`. | The API key validates on US1. |
| Atlas cluster | Shared cluster `workshopai`, database `hanlane`. The `workshop` database on the same cluster is not touched. Quotas (100 ops/s, 512 MB, 10 GB per 7 days) are shared with it. | Owner's choice for the hackathon. |
| Process launch | `python -m common.envrun <service> <cmd>` loads `.env` and sets `DD_VERSION` to the git sha. | Sourcing `.env` in a shell breaks on URLs containing `&`. |
| Cut up front | Hanoi traffic mode, replay, RSU-based LANE_JAM, Datadog MCP scene, second map key. | Cut order in CLAUDE.md; the user asked for all 5 days in one pass. |
| M0 write ceiling | Measured (`docs/measure_m0*.json`): `insert_many` of 50 docs/s takes ~145 ms, 100-200 docs ~0.9 s, 300 docs ~2 s; 150 `insert_one`/s reach only ~70/s with p50 0.9 s. `serverStatus.opcounters.insert` counts documents, not commands. The throttle behaves like ~100 documents per second, so batching saves round trips but does not lift the ceiling. | CLAUDE.md assumed one `insert_many` counts as one operation; the measurement does not confirm it on this cluster. |
| Fleet size | ~60 vehicles (`sim.scale = 0.3`), well under the ~100 docs/s ceiling. The x3 load test (~180) is expected to exceed it. | Follows from the ceiling above. |
| Telemetry TTL | Kept at 7200 s. Measured 249 bytes per record on disk including indexes (the 2dsphere index is larger than the data); at <100 vehicles two hours stay under ~210 MB including the ~17 min TTL lag. 300 vehicles would need ~3000 s. | Measured on day 1. |
| Fast path | Enabled (`[loop] fast_path = true`): the gateway POSTs each event to the worker right after `events.insert_one`; the change stream stays on as the durable fallback and its lag is reported as `hanlane.changestream.lag_ms`. Whichever path arrives first dispatches (dedup by event id). | Measured change stream t3->t4 p95 was 539 ms against a 400 ms budget; with the fast path t3->t4 p95 is ~6 ms and the loop p95 ~200 ms (bounded by the 0.2 s SUMO step). |
| Incident edge | `295386151` (Dragon Bridge, 3 lanes, 583 m), detected by street name in `sim/build_net.py`. A denser flow crosses it so incidents have followers. | |
| STALLED near junctions | Not fired when the vehicle is within 15 m of the end of its lane. | Vehicles yielding at unsignalised junctions were reported as stalled. |
| Web port | 5180 (`--strictPort`). | Another local app already listens on 5173. |
